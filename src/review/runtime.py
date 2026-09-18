"""定向复核的模型运行边界。

保持默认渠道和允许渠道的单一来源，避免 API、后台执行器与
复核 agent 各自漂移。
"""

DEFAULT_REVIEW_MODEL_GROUP = "gptload"
REVIEW_MODEL_GROUPS = frozenset({"gptload", "cunai", "full", "light", "mimo"})
