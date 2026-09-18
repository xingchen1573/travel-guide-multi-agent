"""全局配置：模型provider、路径常量，从 .env 读取。

.env 用的是通用的 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL 命名（不叫 ANTHROPIC_API_KEY），
因为当前配置的是 OpenAI 兼容的 relay 端点（SenseNova），不是 Anthropic 原生 API。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

from travel_guide_kernel.paths import resolve_runtime_root

load_dotenv()

CODE_ROOT = Path(__file__).resolve().parent.parent
# Historical public name retained for code and version-controlled resources.
PROJECT_ROOT = CODE_ROOT
# Primary runtime state shares this process-level root when configured.
RUNTIME_ROOT = resolve_runtime_root(CODE_ROOT)
DATA_DIR = RUNTIME_ROOT / "data"
RUNTIME_DIR = RUNTIME_ROOT / "runtime"
PROJECTS_DIR = RUNTIME_ROOT / "projects"
MATERIAL_LIBRARY_DIR = DATA_DIR / "material_library"
AGENTS_DIR = CODE_ROOT / "src" / "agents"

LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL")
LLM_MODEL = os.environ.get("LLM_MODEL")

# 对话模型请求超时（秒）。飞书接入经验文档记录的教训①：长连接/长会话场景下，模型客户端
# 没有显式超时会导致"任意一轮对话可能永久挂起"——上游一旦变慢，调用方会无限期占住线程且
# 没有任何异常可捕获。这个项目此前一直没设置过，趁飞书WS worker接入前先补上，跟429轮询
# 重试（src/llm_rotation.py）是两回事：轮询处理"请求失败后怎么重试"，超时处理"请求
# 迟迟不返回时怎么主动放弃"。
CHAT_MODEL_TIMEOUT_SECONDS = float(os.environ.get("CHAT_MODEL_TIMEOUT_SECONDS", "600"))

# 轻量分类/抽取请求通常数秒完成，不应继承长攻略模型的600秒容忍窗口。峨眉山
# 真实E2E暴露：候选标题粗筛中一条原本8-15秒的light请求挂起后，全局600秒
# timeout会让自动补采长时间无输出。单独收窄到60秒，超时后仍由既有key轮询/放行
# 降级处理，不影响MiMo长输出。
LIGHT_MODEL_TIMEOUT_SECONDS = float(os.environ.get("LIGHT_MODEL_TIMEOUT_SECONDS", "60"))

# 多模态模型（用户提交图文素材时描述图片用，见src/materials/collectors/user_submit.py）。
# 和LLM_MODEL同一个relay端点(SenseNova)，只是换模型名，复用同一套LLM_API_KEYS轮询机制。
LLM_VISION_MODEL = os.environ.get("LLM_VISION_MODEL", "sensenova-6.7-flash-lite")

# 轻量模型（简单分类/字段抽取用，见get_light_chat_model）。和LLM_MODEL同一个relay端点，
# 只是换成更便宜的模型名，同样复用LLM_API_KEYS轮询机制。默认直接复用已经验证过能在这个
# relay端点跑通的sensenova-6.7-flash-lite（跟LLM_VISION_MODEL默认值相同，但这是两个独立
# 的环境变量——多模态模型未来如果换成专门的图片理解模型，不应该连带影响纯文本轻量调用）。
LLM_LIGHT_MODEL = os.environ.get("LLM_LIGHT_MODEL", "sensenova-6.7-flash-lite")

# 多key轮询（见docs/ISSUES.md）：LLM_API_KEYS用逗号分隔多个key，遇到429限流时
# src.llm_rotation会自动换下一个key重试。未设置时退化成[LLM_API_KEY]单元素列表，
# 完全兼容现有单key场景——旧.env不用改，"轮询"退化成"原地等待重试"。
def _parse_api_keys(keys_raw: str | None, single_key: str | None) -> list[str]:
    """纯函数，方便单测（不依赖环境变量/模块重载）：
    keys_raw是LLM_API_KEYS原始字符串（可能是None），single_key是LLM_API_KEY。
    """
    if keys_raw:
        keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
        if keys:
            return keys
    return [single_key] if single_key else []


_LLM_API_KEYS_RAW = os.environ.get("LLM_API_KEYS")
LLM_API_KEYS = _parse_api_keys(_LLM_API_KEYS_RAW, LLM_API_KEY)

# 轻量模型分组独立key池（管理后台"模型/Key"页面讨论定案：full/light两组各自独立
# 轮询指针+独立key池，不再共用一个_current_key_index，见docs/ARCHITECTURE.md第13.2节）。
# LLM_API_KEYS_LIGHT未配置时直接回退到LLM_API_KEYS（跟旧行为完全一致，退化成
# "light组也用主力key池"）。
_LLM_API_KEYS_LIGHT_RAW = os.environ.get("LLM_API_KEYS_LIGHT")
LLM_API_KEYS_LIGHT = _parse_api_keys(_LLM_API_KEYS_LIGHT_RAW, None) or LLM_API_KEYS

AMAP_MCP_API_KEY = os.environ.get("AMAP_MCP_API_KEY")  # 未配置时 amap_query 工具走stub

# 森林页真实地图用（呈现层P2第一项，见src/tools/geocode.py、run_generate_site.py）。
# 跟AMAP_MCP_API_KEY是不同用途的两个key（高德开放平台里"Web服务API"和"Web端(JS API)"
# 是分别申请的key类型），不能混用：
# - AMAP_GEOCODE_API_KEY：服务端调用地理编码REST接口（POI名称->经纬度）。
# - AMAP_JS_API_KEY：前端网页里加载高德地图JS API画点位用，暴露在生成的HTML里，
#   申请时需要配置referer白名单，不是敏感的服务端凭证。
# 未配置时地图区域降级为POI列表展示，跟AMAP_MCP_API_KEY同一种优雅降级模式。
AMAP_GEOCODE_API_KEY = os.environ.get("AMAP_GEOCODE_API_KEY")
AMAP_JS_API_KEY = os.environ.get("AMAP_JS_API_KEY")
# AMAP_JS_API_KEY的配套"安全密钥"（高德2021年底起JS API强制要求，跟key一起在控制台生成）。
# 注意这个不是"未配置就降级"——AMAP_JS_API_KEY配了但这个漏配，地图会显式报
# INVALID_USER_SCODE加载失败，不是静默降级成POI列表，详见docs/ISSUES.md。
AMAP_JS_SECURITY_CODE = os.environ.get("AMAP_JS_SECURITY_CODE")

# Tavily搜索API（实时agent用，查天气/营业状态/最新事件这类真正需要联网的时效性信息）。
# 未配置时 web_search 工具走stub，跟AMAP_MCP_API_KEY是同一种"没有真实key时优雅降级"模式。
# 支持TAVILY_API_KEYS逗号分隔多个key轮询（复用上面LLM_API_KEYS同款_parse_api_keys纯函数），
# 免费版单key额度有限，遇到429/额度耗尽时src.tools.web_search_tools会自动换下一个key重试。
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
_TAVILY_API_KEYS_RAW = os.environ.get("TAVILY_API_KEYS")
TAVILY_API_KEYS = _parse_api_keys(_TAVILY_API_KEYS_RAW, TAVILY_API_KEY)

EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL")

# DeepSeek官方API（2026-07-31新增，见docs/ISSUES.md）：排查青甘大环线（7天长行程）
# lifestyle_agent反复"多次尝试后仍未完成写入操作"崩溃时，诊断发现商汤SenseNova免费公测
# 渠道对deepseek-v4-flash存在额外的服务端限制——同样的真实prompt，商汤渠道reasoning
# token经常飙到2000-3800并卡在8193附近截断，DeepSeek官方直连reasoning只需16-33、
# 全部顺利完成。仅lifestyle_agent这一个撞到问题的调用点切换到官方直连，其余8个agent
# 继续用商汤（LLM_BASE_URL），不做全局切换。
DEEPSEEK_OFFICIAL_API_KEY = os.environ.get("DEEPSEEK_OFFICIAL_API_KEY")
DEEPSEEK_OFFICIAL_BASE_URL = os.environ.get("DEEPSEEK_OFFICIAL_BASE_URL", "https://api.deepseek.com")

# MiMo（ASR语音转写用sandbox/mimo_asr、src/materials/collectors/bilibili.py；
# 对话模型用于lifestyle_agent，见docs/ISSUES.md青甘大环线诊断记录）。ASR调用和对话模型
# 调用共用同一个MIMO_API_KEYS池——2026-07-31用户决定所有MiMo key不分用途统一轮询共享，
# 跟LLM_API_KEYS/TAVILY_API_KEYS同一个动机，遇到限流/额度耗尽时换下一个key重试。
# 第一个key（列表首位）优先使用，轮询顺序即列表顺序，不额外做"优先key用完才轮询"这种
# 分级——按到达顺序排列MIMO_API_KEYS本身就表达了优先级（先配的先用）。
MIMO_API_KEY = os.environ.get("MIMO_API_KEY")
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_CHAT_BASE_URL = os.environ.get("MIMO_CHAT_BASE_URL")
MIMO_ASR_MODEL = os.environ.get("MIMO_ASR_MODEL", "mimo-v2.5-asr")
MIMO_CHAT_MODEL = os.environ.get("MIMO_CHAT_MODEL", "mimo-v2.5")
MIMO_PRO_CHAT_MODEL = os.environ.get("MIMO_PRO_CHAT_MODEL", "mimo-v2.5-pro")
_MIMO_API_KEYS_RAW = os.environ.get("MIMO_API_KEYS")
MIMO_API_KEYS = _parse_api_keys(_MIMO_API_KEYS_RAW, MIMO_API_KEY)

# 可选 gpt-load 聚合入口。聚合层在服务端调度不同余额的官方 key；未配置 key 时
# 不会进入应用侧的 MiMo 轮询链。
MIMO_GPTLOAD_API_KEY = os.environ.get("MIMO_GPTLOAD_API_KEY")
_MIMO_GPTLOAD_API_KEYS_RAW = os.environ.get("MIMO_GPTLOAD_API_KEYS")
MIMO_GPTLOAD_API_KEYS = _parse_api_keys(_MIMO_GPTLOAD_API_KEYS_RAW, MIMO_GPTLOAD_API_KEY)
MIMO_GPTLOAD_BASE_URL = os.environ.get("MIMO_GPTLOAD_BASE_URL") or MIMO_CHAT_BASE_URL

# GPT-Load是Provider，模型名独立配置。新命名优先，旧MIMO_*变量只做兼容回退，
# 避免以后在DeepSeek/GLM等模型之间切换时还要改URL、Key池或Trace分组。
GPTLOAD_API_KEY = os.environ.get("GPTLOAD_API_KEY") or MIMO_GPTLOAD_API_KEY
_GPTLOAD_API_KEYS_RAW = os.environ.get("GPTLOAD_API_KEYS")
GPTLOAD_API_KEYS = (
    _parse_api_keys(_GPTLOAD_API_KEYS_RAW, GPTLOAD_API_KEY)
    or MIMO_GPTLOAD_API_KEYS
)
GPTLOAD_BASE_URL = (
    os.environ.get("GPTLOAD_BASE_URL")
    or os.environ.get("MIMO_GPTLOAD_BASE_URL")
    or MIMO_CHAT_BASE_URL
)
GPTLOAD_CHAT_MODEL = os.environ.get(
    "GPTLOAD_CHAT_MODEL",
    os.environ.get("DEEPSEEK_CHAT_MODEL", "glm-5.3-flash"),
)

# jiji.cc OpenAI兼容中转。凭证只从环境变量读取，不进入历史mimo/GPT-Load运行时
# Key池，避免两个Provider共享同一base_url或在Trace里互相冒名。默认主链仍是GPT-Load；
# 只有启动进程显式设置TRAVEL_MODEL_PROVIDER=jiji时才会选中本组。
JIJI_API_KEY = os.environ.get("JIJI_API_KEY")
_JIJI_API_KEYS_RAW = os.environ.get("JIJI_API_KEYS")
JIJI_API_KEYS = _parse_api_keys(_JIJI_API_KEYS_RAW, JIJI_API_KEY)
JIJI_BASE_URL = os.environ.get("JIJI_BASE_URL", "https://www.jiji.cc/v1")
JIJI_CHAT_MODEL = os.environ.get("JIJI_CHAT_MODEL", "glm-5.3-flash")
TRAVEL_MODEL_PROVIDER = os.environ.get("TRAVEL_MODEL_PROVIDER", "gptload").strip().lower()
if TRAVEL_MODEL_PROVIDER not in {"gptload", "jiji"}:
    raise ValueError(
        "TRAVEL_MODEL_PROVIDER仅支持gptload或jiji，"
        f"当前为{TRAVEL_MODEL_PROVIDER!r}"
    )

# POI首次生成在GPT-Load完整技术退避耗尽后的独立恢复渠道。Provider和模型分开配置，
# 便于后续替换模型而不改恢复编排；未配置时保持旧行为，恢复实例继续使用GPT-Load。
OOIOO_API_KEY = os.environ.get("OOIOO_API_KEY")
_OOIOO_API_KEYS_RAW = os.environ.get("OOIOO_API_KEYS")
OOIOO_API_KEYS = _parse_api_keys(_OOIOO_API_KEYS_RAW, OOIOO_API_KEY)
OOIOO_BASE_URL = os.environ.get("OOIOO_BASE_URL", "https://ooioo.work/v1")
OOIOO_CHAT_MODEL = os.environ.get("OOIOO_CHAT_MODEL", "deepseek-v4-flash")

# 历史名称继续可读，调用热路径使用上面的Provider命名。
DEEPSEEK_GPTLOAD_BASE_URL = GPTLOAD_BASE_URL
DEEPSEEK_CHAT_MODEL = GPTLOAD_CHAT_MODEL

# MiMo Token Plan渠道（2026-08-01新增，见docs/ISSUES.md：emeishan_3d评测真实撞到旧
# sk-渠道402余额耗尽，导致run_plan/lifestyle_agent直接崩溃）。域名/key前缀（tp-开头）
# 均与上面的旧sk-渠道不同，是独立开的账户。src.llm_rotation的"mimo"分组现在优先用这个
# 渠道，每个key都耗尽/限流后才回退到旧sk-渠道（MIMO_API_KEYS），而不是二选一切换——
# 这样两个渠道任一个还有余额，对话模型调用就不会中断。
_MIMO_TOKEN_PLAN_API_KEYS_RAW = os.environ.get("MIMO_TOKEN_PLAN_API_KEYS")
MIMO_TOKEN_PLAN_API_KEY = os.environ.get("MIMO_TOKEN_PLAN_API_KEY")
MIMO_TOKEN_PLAN_API_KEYS = _parse_api_keys(_MIMO_TOKEN_PLAN_API_KEYS_RAW, MIMO_TOKEN_PLAN_API_KEY)
MIMO_TOKEN_PLAN_BASE_URL = os.environ.get("MIMO_TOKEN_PLAN_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")

# 硅基流动 ASR（2026-08-04新增，见docs/ISSUES.md第N条）：B站/抖音采集pipeline的
# ASR转写主渠道。用户2026-08-04决定ASR从MiMo切到硅基流动，MiMo保留作兜底。两者接口
# 形态不同——硅基走标准Whisper风格的 /v1/audio/transcriptions multipart上传，MiMo走
# chat.completions.create 的 input_audio data URL 风格，所以 sandbox/mimo_asr/
# test_asr.py 内部按渠道分两路调用，对调用方仍暴露同一个 transcribe_text(audio_path, lang) 签名。
#
# 硅基渠道具体模型2026-08-24从TeleAI/TeleSpeechASR切到XingChenAGI/XingChenASR-V3.2-Ultra
# （同平台同接口，实测10分50秒/15.6MB mp3整段直转68秒、200状态码、文本连贯无复读）。
#
# 实测：硅基渠道一次可吃 20MB+ wav（10分钟视频），不需要切片，且不像 MiMo 那样
# 在长序列尾部退化复读——transcribe_long.py 在硅基渠道下走整段直转路径，只有回退到 MiMo
# 兜底时才启用原来的切片+dedup 逻辑（MiMo base64 有 10MB 硬限制+长序列生成退化）。
#
# 跟 SILICONFLOW_LLM_* / EMBEDDING_* 同一个平台（api.siliconflow.cn），但这是独立申请的 ASR key，
# 不和对话模型/embedding 共用——ASR 走 /v1/audio/transcriptions，跟对话 /v1/chat/completions
# 是不同的接口，key 各自独立计费。多 key 逗号分隔轮询，跟 LLM_API_KEYS 同一个动机。
SILICONFLOW_ASR_API_KEY = os.environ.get("SILICONFLOW_ASR_API_KEY")
SILICONFLOW_ASR_BASE_URL = os.environ.get("SILICONFLOW_ASR_BASE_URL", "https://api.siliconflow.cn/v1")
SILICONFLOW_ASR_MODEL = os.environ.get("SILICONFLOW_ASR_MODEL", "XingChenAGI/XingChenASR-V3.2-Ultra")
_SILICONFLOW_ASR_API_KEYS_RAW = os.environ.get("SILICONFLOW_ASR_API_KEYS")
SILICONFLOW_ASR_API_KEYS = _parse_api_keys(_SILICONFLOW_ASR_API_KEYS_RAW, SILICONFLOW_ASR_API_KEY)

# cun.ai 直连渠道（2026-08-11新增）：独立key池+独立base_url+独立默认模型，不接入
# runtime_config运行时模型覆盖。端点已实测（curl）：POST /v1/chat/completions 简单对话/
# tool_calls/35000字符大prompt（约1.86万token）全部通过，3-4秒返回。已知行为：
# 是reasoning模型，max_tokens给太小（如20）会被reasoning
# 挤占导致content截断，需要给够max_tokens（500+验证过没问题）。
CUNAI_API_KEY = os.environ.get("CUNAI_API_KEY")
CUNAI_BASE_URL = os.environ.get("CUNAI_BASE_URL", "https://www.cun.ai/v1")
CUNAI_CHAT_MODEL = os.environ.get("CUNAI_CHAT_MODEL", "deepseek-v4-flash-0731")
_CUNAI_API_KEYS_RAW = os.environ.get("CUNAI_API_KEYS")
CUNAI_API_KEYS = _parse_api_keys(_CUNAI_API_KEYS_RAW, CUNAI_API_KEY)

# 概览海报生成（呈现层P2第三项，见run_generate_poster.py）。不做联网搜图，直接用生图模型
# 一次性生成整张信息图海报，经cun.ai中转访问gpt-image-2。跟AMAP_MCP_API_KEY等不是同一种
# "静默stub降级"模式——这是用户手动触发的一次性CLI操作，未配置key时应直接报错提示配置，
# 不需要在主链路里兜底。
POSTER_IMAGE_API_KEY = os.environ.get("POSTER_IMAGE_API_KEY")
POSTER_IMAGE_BASE_URL = os.environ.get("POSTER_IMAGE_BASE_URL", "https://www.cun.ai/v1")
POSTER_IMAGE_MODEL = os.environ.get("POSTER_IMAGE_MODEL", "gpt-image-2")


def get_chat_model(
    temperature: float = 0.3,
    max_tokens: int | None = None,
    *,
    streaming: bool = False,
):
    """返回配置好的 ChatOpenAI 实例（带429限流自动换key重试，见src.llm_rotation），
    所有agent共用这一个工厂函数。

    max_tokens：透传给build_chat_model的sensetime_max_tokens，只在落到商汤(full)渠道时
    生效（见docs/ISSUES.md第123条）——citation_check.py的alias_judge/content_judge这类
    判断类调用默认不传（不影响绝大多数短回复场景），需要防护reasoning总量截断
    （第82/83/122条同款问题）的调用点显式传32000。"""
    from src.llm_rotation import build_chat_model

    return build_chat_model(
        temperature,
        sensetime_max_tokens=max_tokens,
        streaming=streaming,
    )


def get_structured_chat_model(
    temperature: float = 0,
    max_tokens: int | None = None,
    disable_reasoning: bool = False,
):
    """Return the shared model boundary for narrow structured travel tasks.

    Material extraction, evidence judgments, timeliness checks and route
    feasibility decisions all use this factory. Keeping them on one explicit
    provider/model boundary prevents small JSON tasks from drifting back to the
    full SenseNova channel while ordinary ReAct agents retain their own policy.
    ``max_tokens`` and ``disable_reasoning`` are request-scoped controls and
    therefore remain active for either supported primary travel Provider. The
    model boundary sends the Provider-native ``max_tokens`` body field rather
    than LangChain's normalized ``max_completion_tokens`` field.
    """
    from src.llm_rotation import build_verification_chat_model

    return build_verification_chat_model(
        temperature=temperature,
        request_max_tokens=max_tokens,
        disable_reasoning=disable_reasoning,
    )


def get_verification_chat_model(
    temperature: float = 0,
    max_tokens: int | None = None,
    disable_reasoning: bool = False,
):
    """Compatibility name for semantic quality-gate call sites."""
    return get_structured_chat_model(
        temperature=temperature,
        max_tokens=max_tokens,
        disable_reasoning=disable_reasoning,
    )


def get_mimo_verification_chat_model(temperature: float = 0):
    """Legacy compatibility name; the runtime boundary is now DeepSeek."""
    return get_structured_chat_model(
        temperature=temperature,
    )


def get_vision_model(temperature: float = 0.3):
    """返回配置好的多模态模型实例（描述图片用），跟get_chat_model同一个relay端点，
    只是模型名换成LLM_VISION_MODEL，同样带429限流自动换key重试。"""
    from src.llm_rotation import build_chat_model

    return build_chat_model(temperature, model=LLM_VISION_MODEL)


def get_light_chat_model(temperature: float = 0.3):
    """返回配置好的轻量模型实例，跟get_chat_model同一个relay端点，只是模型名默认换成
    LLM_LIGHT_MODEL，且走独立的"light"分组——独立key池（LLM_API_KEYS_LIGHT）+独立轮询
    指针+独立的管理后台运行时模型覆盖，不再和"full"分组（9个业务agent）共用同一份状态，
    见docs/ARCHITECTURE.md第13.2节。

    用于简单分类/固定字段抽取这类"多选一/结构化抽取"任务（比如从一句话里抠
    destination/days等字段、判断用户意图落在哪个候选类别），不用于核心内容生成
    或引用真实性核验这类质量门禁环节——后者统一走
    get_structured_chat_model 的 DeepSeek 结构化边界。
    """
    from src.llm_rotation import build_chat_model

    return build_chat_model(temperature, group="light")


def get_embeddings():
    """返回配置好的 embedding 客户端。独立于 get_chat_model 的provider，
    因为对话模型的relay端点不一定提供embeddings接口（目前SenseNova就没有）。
    """
    from langchain_openai import OpenAIEmbeddings

    if not (EMBEDDING_API_KEY and EMBEDDING_BASE_URL and EMBEDDING_MODEL):
        raise RuntimeError(
            "缺少embedding配置，请检查 .env 中的 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL"
        )

    return OpenAIEmbeddings(
        base_url=EMBEDDING_BASE_URL,
        api_key=EMBEDDING_API_KEY,
        model=EMBEDDING_MODEL,
    )
