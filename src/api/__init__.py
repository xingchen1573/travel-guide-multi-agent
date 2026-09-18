"""管理后台后端API（FastAPI）。只读接口直接复用src/workspace/store.py的读函数，
不新增第二套读数据逻辑；唯一的写操作（重跑project）见jobs.py。
"""
