#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import pandas as pd
import sqlite3
import sys
import os
from typing import List, Dict

def process_multi_header_csv(csv_file_path: str, encoding: str = 'utf-8') -> pd.DataFrame:
    """
    3줄 헤더를 가진 CSV 파일을 처리하여 DataFrame으로 변환
    
    Args:
        csv_file_path: CSV 파일 경로
        encoding: 파일 인코딩 (기본값: utf-8, cp949나 euc-kr도 시도 가능)
    
    Returns:
        처리된 DataFrame
    """
    try:
        # CSV 파일 읽기 (헤더 없이)
        df = pd.read_csv(csv_file_path, header=None, encoding=encoding)
        
        # 첫 3줄을 헤더로 추출
        header_row1 = df.iloc[0].fillna('').astype(str)  # 큰 카테고리
        header_row2 = df.iloc[1].fillna('').astype(str)  # 메인 헤더  
        header_row3 = df.iloc[2].fillna('').astype(str)  # 부가 정보 (단위 등)
        
        # 줄바꿈 문자 제거 및 정리
        header_row1 = header_row1.str.replace('\n', ' ').str.replace('\r', ' ').str.strip()
        header_row2 = header_row2.str.replace('\n', ' ').str.replace('\r', ' ').str.strip()
        header_row3 = header_row3.str.replace('\n', ' ').str.replace('\r', ' ').str.strip()
        
        # 새로운 컬럼명 생성
        new_columns = []
        current_category = ''
        column_count = {}  # 중복 컬럼명 카운터
        
        for i in range(len(header_row2)):
            # 큰 카테고리가 있는 경우 업데이트 ("-"인 경우 무시)
            category = header_row1[i].strip()
            if category and category != '-':
                current_category = category
            elif category == '-':
                current_category = ''  # "-"인 경우 카테고리 초기화
            
            main_header = header_row2[i].strip()
            sub_info = header_row3[i].strip()
            
            # 컬럼명 조합 (current_category가 비어있거나 "-"인 경우 무시)
            if current_category and main_header:
                if sub_info:
                    column_name = f"{current_category}_{main_header}_{sub_info}"
                else:
                    column_name = f"{current_category}_{main_header}"
            elif main_header:
                if sub_info:
                    column_name = f"{main_header}_{sub_info}"
                else:
                    column_name = main_header
            else:
                column_name = f"column_{i}"
            
            # 특수문자 제거 및 정리 (괄호, 콜론 등 포함)
            column_name = (column_name.replace(' ', '_')
                          .replace('(', '_')
                          .replace(')', '_')
                          .replace('/', '_')
                          .replace('-', '_')
                          .replace(':', '_')
                          .replace(',', '_')
                          .replace('.', '_'))
            
            # 연속된 언더스코어 제거 및 앞뒤 언더스코어 제거
            while '__' in column_name:
                column_name = column_name.replace('__', '_')
            column_name = column_name.strip('_')
            
            # 중복 컬럼명 처리
            original_name = column_name
            if column_name in column_count:
                column_count[column_name] += 1
                column_name = f"{original_name}_{column_count[column_name]}"
            else:
                column_count[column_name] = 0
            
            new_columns.append(column_name)
        
        # 데이터 부분만 추출 (4번째 줄부터)
        data_df = df.iloc[3:].reset_index(drop=True)
        data_df.columns = new_columns
        
        # "색인"이 포함된 컬럼 제거
        columns_to_keep = [col for col in data_df.columns if '색인' not in col]
        removed_columns = [col for col in data_df.columns if '색인' in col]
        
        if removed_columns:
            print(f"제거된 색인 컬럼: {removed_columns}")
        
        data_df = data_df[columns_to_keep]
        
        # 빈 행 제거
        data_df = data_df.dropna(how='all')
        
        print(f"처리된 데이터: {len(data_df)} 행, {len(data_df.columns)} 열")
        print(f"컬럼명: {list(data_df.columns)[:5]}...")  # 처음 5개 컬럼명만 출력
        
        return data_df
        
    except UnicodeDecodeError:
        # UTF-8로 안되면 다른 인코딩 시도
        if encoding == 'utf-8':
            print("UTF-8 인코딩 실패, cp949로 재시도...")
            return process_multi_header_csv(csv_file_path, encoding='cp949')
        elif encoding == 'cp949':
            print("cp949 인코딩 실패, euc-kr로 재시도...")
            return process_multi_header_csv(csv_file_path, encoding='euc-kr')
        else:
            raise

def save_to_sqlite(df: pd.DataFrame, db_path: str, table_name: str = 'nutrition_data'):
    """
    DataFrame을 SQLite 데이터베이스에 저장
    
    Args:
        df: 저장할 DataFrame
        db_path: SQLite 데이터베이스 파일 경로
        table_name: 테이블 이름
    """
    try:
        # 컬럼 타입 설정
        # 문자열 컬럼 패턴 정의 (식품군, 식품명, 출처 등)
        string_patterns = ['식품군', '식품명', '출처', '학목', '색인']
        
        # 데이터 타입 처리
        df_processed = df.copy()
        column_types = {}
        
        for col in df_processed.columns:
            is_string_column = any(pattern in col for pattern in string_patterns)
            
            if is_string_column:
                # 문자열 컬럼 처리
                df_processed[col] = df_processed[col].astype(str)
                column_types[col] = 'TEXT'
            else:
                # 숫자 컬럼 처리 (소수점 가능)
                df_processed[col] = pd.to_numeric(df_processed[col], errors='coerce')
                column_types[col] = 'REAL'
        
        # SQLite 연결
        conn = sqlite3.connect(db_path)
        
        # 테이블 생성을 위한 SQL 스키마 생성
        create_table_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ("
        column_definitions = []
        
        for col, dtype in column_types.items():
            # 컬럼명에 특수문자가 있는 경우 따옴표로 감싸기
            safe_col = f'"{col}"' if any(c in col for c in [' ', '-', '(', ')', '/']) else col
            column_definitions.append(f"{safe_col} {dtype}")
        
        create_table_sql += ", ".join(column_definitions) + ")"
        
        # 기존 테이블 삭제 후 새로 생성
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        conn.execute(create_table_sql)
        
        # 데이터 삽입
        df_processed.to_sql(table_name, conn, if_exists='append', index=False)
        
        # 테이블 정보 확인
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row_count = cursor.fetchone()[0]
        
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = cursor.fetchall()
        
        print(f"\n=== SQLite 저장 완료 ===")
        print(f"데이터베이스: {db_path}")
        print(f"테이블: {table_name}")
        print(f"저장된 행 수: {row_count}")
        print(f"컬럼 수: {len(columns)}")
        
        # 컬럼 타입 정보 출력
        print(f"\n=== 컬럼 타입 정보 ===")
        for col_info in columns:
            col_name, col_type = col_info[1], col_info[2]
            print(f"{col_name}: {col_type}")
        
        # 샘플 데이터 조회
        print(f"\n=== 샘플 데이터 (처음 3행) ===")
        sample_df = pd.read_sql(f"SELECT * FROM {table_name} LIMIT 3", conn)
        print(sample_df.to_string())
        
        conn.close()
        
    except Exception as e:
        print(f"SQLite 저장 중 오류 발생: {e}")
        raise

def main():
    """메인 함수"""
    if len(sys.argv) < 2:
        print("사용법: python script.py <csv_file_path> [db_file_path] [table_name]")
        print("예시: python script.py nutrition_data.csv nutrition.db food_nutrition")
        sys.exit(1)
    
    # 명령행 인자 처리
    csv_file = sys.argv[1]
    db_file = sys.argv[2] if len(sys.argv) > 2 else csv_file.replace('.csv', '.db')
    table_name = sys.argv[3] if len(sys.argv) > 3 else 'nutrition_data'
    
    # 파일 존재 확인
    if not os.path.exists(csv_file):
        print(f"오류: CSV 파일을 찾을 수 없습니다: {csv_file}")
        sys.exit(1)
    
    try:
        print(f"CSV 파일 처리 시작: {csv_file}")
        
        # CSV 처리
        df = process_multi_header_csv(csv_file)
        
        # SQLite에 저장
        save_to_sqlite(df, db_file, table_name)
        
        print(f"\n✅ 변환 완료!")
        print(f"   입력: {csv_file}")
        print(f"   출력: {db_file} (테이블: {table_name})")
        
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()