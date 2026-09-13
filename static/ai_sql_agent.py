import re
import pymysql
from openai import OpenAI

# ================= 配置区 =================
import os
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '041019',         
    'database': 'taobao_analysis',
    'charset': 'utf8mb4'
}

# 告诉 AI 你的表结构（相当于轻量级的 RAG）
TABLE_SCHEMA = """
表名: user_behavior
字段:
- user_id: 用户ID
- item_id: 商品ID
- category_id: 商品类目ID
- behavior_type: 用户行为类型，pv=浏览, fav=收藏, cart=加购, buy=购买
- timestamp: 时间戳
- date: 日期时间
- date_only: 日期（仅年月日）
"""

# ================= 初始化 =================
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"   # DeepSeek 的 API 地址
)

# ================= 工具函数 =================
def clean_sql(text):
    """去掉 AI 可能输出的 markdown 标记，只保留纯 SQL"""
    text = text.strip()
    text = re.sub(r'^```sql\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'```$', '', text)
    return text.strip()

def execute_sql(sql):
    """连接 MySQL 执行 SQL，返回列名和结果"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            result = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
        return columns, result
    finally:
        conn.close()

# ================= 核心 Agent =================
def ask_ai_agent(question):
    print("\n" + "="*60)
    print(f"🙋 你的问题：{question}")
    print("="*60)

    # 第 1 步：让 AI 生成 SQL
    prompt_sql = f"""
你是资深 SQL 专家。请根据以下表结构，把用户的自然语言问题转换为 MySQL 查询语句。
只输出 SQL 语句，不要任何解释，不要 markdown 代码块。

表结构：
{TABLE_SCHEMA}

用户问题：{question}
"""
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt_sql}],
        temperature=0.1
    )
    sql = clean_sql(response.choices[0].message.content)

    print("\n🤖 AI 生成的 SQL：")
    print(sql)
    print("-"*60)

    # 第 2 步：执行 SQL 并获取结果
    try:
        columns, rows = execute_sql(sql)
        print("📊 数据库查询结果（最多显示前 10 行）：")
        for row in rows[:10]:
            print(dict(zip(columns, row)))
        print(f"（共 {len(rows)} 行）")
    except Exception as e:
        print("❌ SQL 执行失败：", e)
        return

    # 第 3 步：让 AI 基于结果生成业务分析
    prompt_analysis = f"""
你是电商数据分析专家。根据以下数据查询结果，给出简短的分析和运营建议。
用户问题：{question}
查询 SQL：{sql}
查询结果（前 10 行）：{rows[:10]}

请用 200 字以内输出业务洞察和优化建议。
"""
    response2 = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt_analysis}],
        temperature=0.7
    )
    analysis = response2.choices[0].message.content

    print("\n🎯 AI 业务洞察：")
    print(analysis)
    print("="*60)

# ================= 主程序 =================
if __name__ == "__main__":
    print("欢迎使用电商数据智能分析助手！输入 'exit' 退出。")
    while True:
        q = input("\n请输入你的问题：")
        if q.lower() == 'exit':
            break
        ask_ai_agent(q)