"""
Plant Operations Report — example data reporting app on Databricks Apps
--------------------------------------------------------------------------
Two tabs:
  1. Report  -- KPI tiles + charts, queried directly via the SQL Warehouse.
                This is the "standard report" business users check daily.
  2. Ask Genie -- free-form natural language chat over the same governed
                  data, for the questions the fixed report doesn't answer.

Resources required (bind these in the App's "App resources" UI to match
the keys in app.yaml):
  - SQL Warehouse -> SQL_WAREHOUSE_ID
  - Genie Space   -> GENIE_SPACE_ID
"""

import os

import pandas as pd
import plotly.express as px
import streamlit as st
from databricks.sdk import WorkspaceClient

st.set_page_config(page_title="Plant Operations Report", layout="wide")

SQL_WAREHOUSE_ID = os.environ["SQL_WAREHOUSE_ID"]
GENIE_SPACE_ID = os.environ["GENIE_SPACE_ID"]

GOLD_TABLE = "poc_catalog.telemetry.machine_health_gold"
ORDERS_TABLE = "poc_catalog.legacy_orders.orders_silver"

w = WorkspaceClient()


# ---------------------------------------------------------------------------
# Helper: run a SQL query against the warehouse and return a DataFrame
# ---------------------------------------------------------------------------
def run_query(sql: str) -> pd.DataFrame:
    statement = w.statement_execution.execute_statement(
        warehouse_id=SQL_WAREHOUSE_ID,
        statement=sql,
    )
    rows = statement.result.data_array or []
    cols = [c.name for c in statement.manifest.schema.columns]
    return pd.DataFrame(rows, columns=cols)


st.title("Plant Operations Report")

tab_report, tab_genie = st.tabs(["Report", "Ask Genie"])

# ---------------------------------------------------------------------------
# TAB 1: Standard report -- KPIs + charts
# ---------------------------------------------------------------------------
with tab_report:
    st.caption("Refreshes on page load. Click Refresh to pull the latest data.")

    if st.button("Refresh", key="refresh_report"):
        st.cache_data.clear()

    @st.cache_data(ttl=30)
    def load_kpis():
        return run_query(f"""
            SELECT
              COUNT(DISTINCT machine_id) AS machine_count,
              SUM(breach_count) AS total_breaches,
              ROUND(AVG(avg_temperature_c), 1) AS avg_temp,
              SUM(CASE WHEN health_status = 'AT_RISK' THEN 1 ELSE 0 END) AS at_risk_windows
            FROM {GOLD_TABLE}
            WHERE window_start >= current_timestamp() - INTERVAL 24 HOURS
        """)

    @st.cache_data(ttl=30)
    def load_health_trend():
        return run_query(f"""
            SELECT window_start, machine_id, avg_temperature_c, avg_vibration_mm_s, health_status
            FROM {GOLD_TABLE}
            WHERE window_start >= current_timestamp() - INTERVAL 24 HOURS
            ORDER BY window_start
        """)

    @st.cache_data(ttl=60)
    def load_orders_by_region():
        return run_query(f"""
            SELECT region, SUM(order_value_usd) AS total_value
            FROM {ORDERS_TABLE}
            GROUP BY region
            ORDER BY total_value DESC
        """)

    kpis = load_kpis()
    if not kpis.empty:
        row = kpis.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active machines (24h)", int(row["machine_count"] or 0))
        c2.metric("Breach events (24h)", int(row["total_breaches"] or 0))
        c3.metric("Avg temperature", f"{row['avg_temp']}°C" if row["avg_temp"] is not None else "—")
        c4.metric("At-risk windows (24h)", int(row["at_risk_windows"] or 0))

    st.divider()

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Temperature trend by machine (24h)")
        trend = load_health_trend()
        if not trend.empty:
            fig = px.line(
                trend, x="window_start", y="avg_temperature_c", color="machine_id",
                labels={"window_start": "Time", "avg_temperature_c": "Avg Temp (°C)"},
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No telemetry in the last 24 hours yet.")

    with col_right:
        st.subheader("Order value by region")
        orders = load_orders_by_region()
        if not orders.empty:
            fig = px.bar(orders, x="region", y="total_value", labels={"total_value": "Order value (USD)"})
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No order data found.")

    st.subheader("Health status breakdown")
    if not trend.empty:
        status_counts = trend["health_status"].value_counts().reset_index()
        status_counts.columns = ["health_status", "count"]
        fig = px.pie(status_counts, names="health_status", values="count", hole=0.4)
        st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------------------
# TAB 2: Ask Genie -- free-form chat over the same governed tables
# ---------------------------------------------------------------------------
with tab_genie:
    st.caption("Ask anything about machine health or historical orders in plain English.")

    if "conversation_id" not in st.session_state:
        st.session_state.conversation_id = None
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    suggestions = [
        "Which machines are currently at risk?",
        "What's our total order value by region?",
        "Show breach trend for MCH-003 over the last day",
    ]
    cols = st.columns(len(suggestions))
    clicked_suggestion = None
    for col, text in zip(cols, suggestions):
        if col.button(text):
            clicked_suggestion = text

    prompt = st.chat_input("Ask a question...") or clicked_suggestion
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.write(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Asking Genie..."):
                if st.session_state.conversation_id is None:
                    response = w.genie.start_conversation_and_wait(
                        space_id=GENIE_SPACE_ID, content=prompt
                    )
                    st.session_state.conversation_id = response.conversation_id
                else:
                    response = w.genie.create_message_and_wait(
                        space_id=GENIE_SPACE_ID,
                        conversation_id=st.session_state.conversation_id,
                        content=prompt,
                    )

                text_parts, query_df = [], None
                for attachment in response.attachments or []:
                    if attachment.text and attachment.text.content:
                        text_parts.append(attachment.text.content)
                    if attachment.query and attachment.query.description:
                        text_parts.append(f"_Query: {attachment.query.description}_")
                        # Pull the actual result rows back for a quick inline chart/table
                        result = w.genie.get_message_attachment_query_result(
                            space_id=GENIE_SPACE_ID,
                            conversation_id=st.session_state.conversation_id,
                            message_id=response.id,
                            attachment_id=attachment.attachment_id,
                        )
                        if result and result.statement_response and result.statement_response.result:
                            data = result.statement_response.result.data_array or []
                            cols_ = [c.name for c in result.statement_response.manifest.schema.columns]
                            query_df = pd.DataFrame(data, columns=cols_)

                answer = "\n\n".join(text_parts) if text_parts else "(no response content)"
                st.write(answer)
                if query_df is not None and not query_df.empty:
                    st.dataframe(query_df, use_container_width=True)

                st.session_state.messages.append({"role": "assistant", "content": answer})
