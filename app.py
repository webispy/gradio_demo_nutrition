from typing import Annotated, TypedDict, Literal, List, Generator, Tuple
import ast
import re
import time
from pprint import pprint

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.vectorstores import InMemoryVectorStore

from langchain_community.tools import QuerySQLDatabaseTool
from langchain_community.utilities import SQLDatabase
from langchain.agents.agent_toolkits import create_retriever_tool

from langchain.chains import create_sql_query_chain
from langchain.chains.sql_database.prompt import SQLITE_PROMPT

from langgraph.graph import START, END, StateGraph 

import gradio as gr
import logging
import sys

##################################################################
# 환경 설정 / 데이터베이스 연결
##################################################################
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("nutrition_assistant")
logger.setLevel(logging.INFO)
logger.propagate = False

# --- levelname 한글자 변환용 ---
LEVEL_MAP = {
    "DEBUG": "D",
    "INFO": "I",
    "WARNING": "W",
    "ERROR": "E",
    "CRITICAL": "F",
}

class AdbStyleFormatter(logging.Formatter):
    def format(self, record):
        record.levelshort = LEVEL_MAP.get(record.levelname, record.levelname[0])
        record.pid = f"{record.process:5d}"
        record.tid = f"{record.thread:5d}"
        return super().format(record)

formatter = AdbStyleFormatter(
    fmt="%(asctime)s.%(msecs)03d %(pid)s %(tid)s %(levelshort)s %(name)s: %(message)s",
    datefmt="%m-%d %H:%M:%S"
)

if not logger.handlers:  # 핸들러 중복 추가 방지
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(formatter)
    logger.addHandler(h)

db = SQLDatabase.from_uri("sqlite:///data/nutrition_data.db")

# 사용 가능한 테이블 목록 출력
tables = db.get_usable_table_names()
logger.info(f"Available tables in the database: {tables}")

columns_result = db.run("SELECT c.name FROM PRAGMA_TABLE_INFO('nutrition_data') c")
logger.info(f"Columns in 'nutrition_data' table: {columns_result}")

data_count = db.run("SELECT COUNT(*) FROM nutrition_data")
logger.info(f"Total records in 'nutrition_data' table: {data_count}")

execute_query_tool = QuerySQLDatabaseTool(db=db)

##################################################################
# 상태 정보 타입 정의
##################################################################

# 상태 정의
class NutritionState(TypedDict):
    question: str
    query: str
    score: float
    result: str
    answer: str
    current_node: str
    status: str


# SQL 쿼리 생성 Structured Output
class QueryOutput(TypedDict):
    """Generated SQL query."""
    query: Annotated[str, ..., "Syntactically valid SQL query."]


# 생성된 쿼리를 평가하기 위한 Structured Output    
class EvaluateOutput(TypedDict):
    """Evaluate SQL query."""
    score: float
    columns: List[str]
    


##################################################################
# 모델 및 체인 생성
##################################################################
llm = ChatOpenAI(model="gpt-4.1-mini")

structured_query_llm = llm.with_structured_output(QueryOutput)
structured_evaluate_llm = llm.with_structured_output(EvaluateOutput)

# SQL 쿼리 생성 chain (최대 10개의 데이터를 가져오는 쿼리 생성)
gpt_sql = create_sql_query_chain(llm=llm, db=db, k=10, prompt=SQLITE_PROMPT)


# 전역 상태 업데이트 콜백
status_callback = None

def set_status_callback(callback):
    """상태 업데이트 콜백 함수 설정"""
    global status_callback
    status_callback = callback

def update_status(node_name: str, description: str, progress: int):
    """상태 업데이트 함수"""
    if status_callback:
        status_callback(node_name, description, progress)


##################################################################
# SQL 쿼리 생성
##################################################################

def write_query(state: NutritionState) -> NutritionState:
    """Generate SQL query to fetch information."""
    update_status("write_query", "🔍 질문을 분석하고 SQL 쿼리를 생성하고 있습니다...", 25)
    
    logger.info("<write_query> Question: %s", state["question"])
    prompt = gpt_sql.invoke({"question": state["question"]})
    result = structured_query_llm.invoke(prompt)    
    logger.info("<write_query> Generated query: %s", result["query"])
    
    return {
        **state,
        "query": result["query"],
        "current_node": "write_query",
        "status": "SQL 쿼리 생성 완료"
    }

def evaluate_query(state: NutritionState) -> NutritionState:
    """Evaluate SQL query."""
    update_status("evaluate_query", "📊 생성된 쿼리의 정합성을 평가하고 있습니다...", 50)
    
    # 실제 구현 부분 (예시)
    prompt = f"""
        아래 질문과 쿼리의 정합성에 대해 평가해주세요. 점수(0~1)로만 평가해주고 사용된 컬럼 이름을 반환해주세요.

        Question: {state["question"]}
        SQLQuery: {state["query"]}
        """
    
    logger.info("<evaluate_query> Prompt: %s", prompt)
    result = structured_evaluate_llm.invoke(prompt)
    logger.info("<evaluate_query> Result: %s", result)

    columns = result["columns"]
    for column in columns:
        if column not in db.get_table_info():
            logger.error(f"사용된 컬럼 {column}이 실제 테이블에 존재하지 않습니다.")
            return {
                **state,
                "score": 0,
                "current_node": "evaluate_query",
                "status": "쿼리 평가 실패 - 잘못된 컬럼"
            }

    return {
        **state,
        "score": result["score"],
        "current_node": "evaluate_query",
        "status": "쿼리 평가 완료"
    }

def decide_next_step(state: NutritionState) -> Literal["execute_query", "unsupported_data"]:
    """점수가 0.3 이상이면 쿼리 실행, 아니면 지원하지 않는 데이터"""
    logger.info("<decide_next_step> score: %s", state['score'])
    if state["score"] > 0.3:
        return "execute_query"
    else:
        return "unsupported_data"

def execute_query(state: NutritionState) -> NutritionState:
    """SQL쿼리 실행"""
    update_status("execute_query", "🧬 데이터베이스에서 영양소 정보를 검색하고 있습니다...", 75)
    
    logger.info("<execute_query> Executing query: %s", state["query"])
    result = execute_query_tool.invoke(state["query"])
    logger.info("<execute_query> Query result: %s", result)

    return {
        **state,
        "result": result,
        "current_node": "execute_query",
        "status": "데이터베이스 검색 완료"
    }

def generate_answer(state: NutritionState) -> NutritionState:
    """주어진 질문, 쿼리, 결과를 바탕으로 답변 생성"""
    update_status("generate_answer", "⚡ 검색 결과를 바탕으로 답변을 생성하고 있습니다...", 90)
    
    prompt = (
        "Given the following user question, corresponding SQL query, "
        "and SQL result, answer the user question.\n\n"
        f'Question: {state["question"]}\n'
        f'SQL Query: {state["query"]}\n'
        f'SQL Result: {state["result"]}'
    )
    
    logger.info("<generate_answer> Prompt: %s", prompt)
    response = llm.invoke(prompt)
    logger.info("<generate_answer> Generated answer: %s", response.content)

    return {
        **state,
        "answer": response.content,
        "current_node": "generate_answer",
        "status": "답변 생성 완료"
    }

def unsupported_data(state: NutritionState) -> NutritionState:
    """지원하지 않는 데이터에 대한 응답"""
    update_status("unsupported_data", "❌ 지원하지 않는 데이터입니다", 100)
    
    logger.info("<unsupported_data> Unsupported data for question: %s", state["question"])

    return {
        **state,
        "answer": "죄송합니다. 현재 데이터베이스에서 해당 질문에 대한 정보를 찾을 수 없습니다.",
        "current_node": "unsupported_data",
        "status": "지원하지 않는 데이터"
    }

##################################################################
# 상태 그래프 생성
##################################################################

def create_graph():
    """StateGraph 생성"""
    graph_builder = StateGraph(NutritionState)

    graph_builder.add_node("write_query", write_query)
    graph_builder.add_node("evaluate_query", evaluate_query)
    graph_builder.add_node("execute_query", execute_query)
    graph_builder.add_node("generate_answer", generate_answer)
    graph_builder.add_node("unsupported_data", unsupported_data)

    graph_builder.add_edge(START, "write_query")
    graph_builder.add_edge("write_query", "evaluate_query")

    graph_builder.add_conditional_edges(
        "evaluate_query",
        decide_next_step
    )

    graph_builder.add_edge("execute_query", "generate_answer")
    graph_builder.add_edge("generate_answer", END)
    graph_builder.add_edge("unsupported_data", END)

    return graph_builder.compile()

##################################################################
# Gradio 인터페이스 - 실시간 상태 표시
##################################################################

def nutrition_assistant_with_status(question: str) -> Generator[Tuple[str, str], None, None]:
    """실시간 상태 업데이트가 포함된 영양소 분석 함수"""
    
    if not question.strip():
        yield "질문을 입력해주세요.", "❌ 빈 질문입니다."
        return
    
    # 현재 상태를 저장할 변수들
    current_status = {"node": "", "description": "", "progress": 0}
    
    def status_update_callback(node_name: str, description: str, progress: int):
        """상태 업데이트 콜백"""
        current_status.update({
            "node": node_name,
            "description": description,
            "progress": progress
        })
    
    # 콜백 설정
    set_status_callback(status_update_callback)
    
    try:
        # 그래프 생성 및 초기 상태 설정
        graph = create_graph()
        initial_state = {
            "question": question,
            "query": "",
            "score": 0.0,
            "result": "",
            "answer": "",
            "current_node": "",
            "status": ""
        }
        
        # 그래프 스트리밍 실행
        final_state = None
        
        for state in graph.stream(initial_state):
            # state는 {node_name: updated_state} 형태
            node_name = list(state.keys())[0]
            node_state = state[node_name]
            final_state = node_state
            
            # 진행률 계산
            progress = current_status["progress"]
            progress_bar = "█" * (progress // 25) + "░" * (4 - progress // 25)
            
            # 중간 결과 표시
            temp_result = f"""
### 🔄 처리 중입니다...

**현재 단계:** {current_status["description"]}

**생성된 SQL 쿼리:** 
```sql
{node_state.get('query', 'N/A')}
```

**평가 점수:** {node_state.get('score', 'N/A')}

**진행 상황:**
```
{progress_bar} {progress}%
```

잠시만 기다려주세요...
            """
            
            status_text = f"{current_status['description']}\n\n진행률: [{progress_bar}] {progress}%"
            
            yield temp_result, status_text
#            time.sleep(0.5)  # 시각적 효과
        
        # 최종 완료 상태 업데이트
        update_status("completed", "✅ 분석 완료!", 100)
        
        # 최종 결과 생성
        if final_state:
            final_result = f"""
### 🍎 영양소 분석 결과

**질문:** {question}

**생성된 SQL 쿼리:**
```sql
{final_state.get('query', 'N/A')}
```

**평가 점수:** {final_state.get('score', 'N/A')}

**검색 결과:**
```
{final_state.get('result', 'N/A')}
```

**최종 답변:**
{final_state.get('answer', '답변을 생성할 수 없습니다.')}

---
✅ **분석 완료** | 국가표준 식품성분표 기준
            """
        else:
            final_result = """
### ❌ 처리 실패

분석 과정에서 오류가 발생했습니다.
다시 시도해주세요.
            """
        
        final_status = "✅ 분석 완료!"
        yield final_result, final_status
        
    except Exception as e:
        error_result = f"""
### ❌ 처리 중 오류 발생

**오류 메시지:** {str(e)}

**질문:** {question}

죄송합니다. 처리 중 오류가 발생했습니다.
다른 질문으로 다시 시도해주세요.
        """
        
        yield error_result, f"❌ 오류 발생: {str(e)}"
    
    finally:
        # 콜백 정리
        set_status_callback(None)

##################################################################
# Gradio 인터페이스
##################################################################

def create_gradio_interface():
    """Gradio 인터페이스 생성"""
    with gr.Blocks(
        title="🍎 식품성분 영양소 조회 어시스턴트",
        theme=gr.themes.Soft()
    ) as demo:
        
        gr.Markdown("""
        # 🍎 식품성분 영양소 조회 어시스턴트
        
        국가 표준 식품 성분 기반 영양소 분석 AI 에이전트입니다.  
        자연어 질문을 SQL 쿼리로 변환하여 정확한 영양소 정보를 제공합니다.
        """)
        
        with gr.Row():
            with gr.Column(scale=1):
                question_input = gr.Textbox(
                    label="🔍 질문을 입력하세요",
                    placeholder="예: 비타민C가 가장 많은 식품 5개는?",
                    lines=4
                )
                
                analyze_btn = gr.Button(
                    "⚡ 영양소 분석하기", 
                    variant="primary", 
                    size="lg"
                )
                
                # 실시간 상태 표시
                status_display = gr.Textbox(
                    label="📊 처리 상태",
                    value="대기 중... 질문을 입력하고 분석 버튼을 클릭하세요.",
                    interactive=False,
                    lines=3
                )
            
            with gr.Column(scale=2):
                result_output = gr.Markdown(
                    value="""
                    ### 📋 분석 결과
                    
                    질문을 입력하고 **분석하기** 버튼을 클릭하면  
                    실시간으로 AI 에이전트의 처리 과정을 확인할 수 있습니다.
                    
                    **처리 단계:**
                    1. 🔍 질문 분석 및 SQL 쿼리 생성
                    2. 📊 쿼리 정합성 평가  
                    3. 🧬 데이터베이스 검색
                    4. ⚡ 답변 생성
                    """,
                    container=True
                )
        
        # 예시 질문
        gr.Markdown("### 💡 예시 질문 (클릭하면 자동 입력)")
        gr.Examples(
            examples=[
                "조리가공식품 중 칼로리가 가장 높은 식품 5개는?",
                "짜장라면과 볶음라면의 당류 함량과 에너지 함량은?",
                "연잎밥에 들어있는 모든 영양소는?",
                "비타민C가 가장 많은 식품 5개는?",
                "오메가3가 가장 많은 식품 5개는?",
                "상위 5개 요오드가 높은 식품은?",
                "칼슘이 풍부한 유제품 종류는?",
                "채소류 중 식이섬유가 가장 많은 식품은?",
                "통풍에 가장 안좋은 식품은?"
            ],
            inputs=question_input
        )
        
        # 버튼 클릭 이벤트 - Generator 함수 연결
        analyze_btn.click(
            fn=nutrition_assistant_with_status,
            inputs=question_input,
            outputs=[result_output, status_display]
        )
        
        # Enter 키 지원
        question_input.submit(
            fn=nutrition_assistant_with_status,
            inputs=question_input,
            outputs=[result_output, status_display]
        )
    
    return demo

if __name__ == "__main__":
    demo = create_gradio_interface()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False
    )
