"""网站呈现层用的POI真实照片（POI名称 -> base64图片数据），供run_generate_site.py给
树木页英雄区配一张代表整个景点的真实图片，替换纯CSS的.img-ph占位框。

跟src/tools/geocode.py/driving.py同一种"没有真实key时优雅降级"模式：未配置
AMAP_GEOCODE_API_KEY、查询失败、候选跟查询词语义不相关时均返回None，调用方据此
保留原有占位框，不阻塞整个网站生成。

跟geocode.py/driving.py同样是纯函数、呈现层专用，不是LangChain @tool——照片数据只在
呈现层流转，不进入agent工具链/LLM token预算。

缓存文件data/material_library/poi_photo_cache.json跨project全局共享（同driving_cache.json
模式），同一个POI在不同project间不重复消耗API额度。只缓存图片URL+二进制数据的base64，
不缓存"未找到/不相关"这类失败结果——避免一次网络抖动或未配key导致的失败被永久缓存，
下次配了key或高德数据更新后应该能重新尝试。

高德POI搜索接口（v3/place/text）返回的photos字段大多是压缩过的小图（实测约500px宽、
30-140KB/张），不需要额外压缩/缩放；这里缓存图片二进制的base64，生成器随后把它
落成site_assets中的完整原图文件，避免把所有图片内嵌进site.html。
但不是接口硬上限——实测偶尔会返回明显更大的图（如1368x1824），说明分辨率取决于该POI
在高德数据库里的收录质量，不是接口统一天花板。据此下载后加了_MIN_PHOTO_WIDTH_PX最小
宽度校验：门槛定得比典型500px更低（480px），只挡真正损坏/异常小的图，不是为了"追求
高清"过滤掉大多数正常结果——高德数据源本身的分辨率上限不是这段代码能解决的问题。

配图粒度：从途经点(segment)级别降级到整个POI级别（2026-08-17真实E2E跑测演变的最终
方案，中间尝试过的四种途经点级方案——city直传/POI名当city/geocode反查城市/周边坐标
搜索——均在真实数据上暴露出图文不符，见下方"演变过程"）。查询词直接用PoiDetailFile
的poi_name本身（如"张掖·七彩丹霞景区""大柴旦翡翠湖"），不带city参数——这类完整景区名
在高德数据库里收录充分、搜索命中率和准确率都远高于内部子设施（"观光塔""望海台站"这类
碎片化途经点名称）。

演变过程（依次真实验证、依次证伪，记录下来避免未来重复踩坑）：
1. 途经点级别+city直传：city清洗成单一城市名后传给高德city参数消歧同名POI——多城市
   接续行程日（"敦煌→嘉峪关→张掖"）的city字段本身就不是单一城市，直传会被高德静默
   忽略退化成全国搜索，产生"正门·西门"→"西门子助听器"这类跨省误配。
2. 途经点级别+POI名当city：把途经点所属的PoiDetailFile.poi_name直接当city参数传——
   高德city参数硬性要求真实城市名/拼音/adcode，景区名不是合法值，同样被静默忽略，
   复现方案1的误配（"观光塔"→保定同名建筑）。
3. 途经点级别+geocode反查城市：先地理编码POI名拿真实城市名再查照片——对"张掖七彩
   丹霞"这类地级市可用，但对"大柴旦翡翠湖"反查出的是30万平方公里的海西州，范围仍
   不够精确（州内跨景点误配：翡翠湖的"观光塔"配成了茶卡盐湖的观光塔）；且地理编码
   本身在缺乏city限定时也会出模糊匹配（"乌素特水上雅丹"被反查到台湾嘉义"水上乡"）。
4. 途经点级别+周边坐标搜索：以POI坐标为圆心、半径5-20公里画圈搜索途经点关键词——
   这类内部子设施在高德数据库里覆盖率本身就低，实测半径搜索大多直接返回空结果，
   不是精度问题，是数据源里根本没收录。

演变结论：途经点级别在长线自驾/多城市行程里几乎不可行——不是校验规则不够严，是
city限定这个维度天然做不到"景点内部子设施"级别的精度，且行政区划越粗，全国/大区域
搜索的误配风险越高。POI整体级别命中率高、风险低，是这次真实验证后收敛到的方案。

文本相关性校验（_is_relevant_match，途经点级和POI级两次真实E2E跑测共同坐实的问题，
不是理论风险）：高德关键词搜索是子串/token模糊匹配，即使候选唯一也可能命中语义完全
不相关的实体。真实复现案例：
- "正门·西门"（杜甫草堂入口）→"西门子助听器(南门中心店)"："西门"是"西门子"前缀子串。
- "景区入口 + 观光车起乘"（张掖七彩丹霞）→"沙坡头旅游景区"：仅共享"景区"这个通用词。
- "望海台站"（大柴旦翡翠湖）→东莞"铜雀台站(公交站)"：仅共享"台站"这个2字通用后缀。
修复用jieba分词而非纯字符子串比对，要求query/candidate分词后至少共享一个长度>=3的
token（外加_GENERIC_STOPWORDS过滤"观光车""游客中心"这类本身就有3字以上的通用词）；
"大廨"这类生僻词分词后每个token都不足3字的情况，靠"候选完整包含query或反之"这条
兜底规则通过（"杜甫草堂-大廨"整体包含"大廨"）。门槛从最初的2字提到3字的原因：
"望海台站"vs"铜雀台站"这类误判无一例外出在2字token上，真实场景里具体地名标识
（"汉昭烈""七彩丹霞"）几乎都在3字以上，拉高门槛比持续往停用词表加词更系统。

QPS节流：高德基础搜索服务QPS上限3次/秒，_throttle()在每次真实网络请求前固定sleep，
避免触发限流错误码。
"""
from __future__ import annotations

import base64
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jieba
import requests
from PIL import Image, UnidentifiedImageError

from src.config import AMAP_GEOCODE_API_KEY, MATERIAL_LIBRARY_DIR

_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
_CACHE_PATH = MATERIAL_LIBRARY_DIR / "poi_photo_cache.json"
_THROTTLE_SECONDS = 0.4  # 固定间隔节流，低于高德3次/秒QPS上限留出余量
_MIN_PHOTO_WIDTH_PX = 480  # 低门槛，只挡异常小/损坏的图，不影响现有典型500px图的覆盖率


def _throttle() -> None:
    time.sleep(_THROTTLE_SECONDS)


# 旅游景区/交通基础设施通用词——几乎任何景点都可能出现，单独重叠不构成"这是同一个
# 地方"的有效信号，是长度>=3主规则之外的补充防线（见_is_relevant_match），覆盖
# "观光车""游客中心"这类本身就有3字以上的通用词——单纯拉高长度门槛防不住它们。
# 不是穷举列表，覆盖已真实观察到的失败模式；未来遇到新的通用词误判案例应补充进来。
_GENERIC_STOPWORDS = frozenset({
    "景区", "景点", "公园", "入口", "出口", "大门", "广场", "购票", "检票", "售票",
    "停车场", "停车", "游客中心", "游客", "中心", "观光车", "摆渡车", "索道", "缆车",
    "码头", "厕所", "公厕", "车站", "大厅", "展厅", "展示中心", "观景台", "观景",
    "平台", "服务区", "休息区", "登山口", "起点", "终点",
})


def _is_relevant_match(query: str, candidate_name: str) -> bool:
    """判断查询词跟高德返回的POI候选名称是否语义相关，而不是恰好字符子串重叠。

    兜底规则：候选名称完整包含查询词，或查询词完整包含候选名称（处理"大廨"这类
    生僻词分词后每个token都不足3字、纯token重叠会误杀真实匹配的情况——"大廨"分词
    是["大","廨"]两个单字，但"杜甫草堂-大廨"整体包含"大廨"，靠这条兜底规则通过）。

    主规则：都不满足包含关系时，用jieba分词各自切出token集合，过滤掉_GENERIC_STOPWORDS
    后要求至少共享一个长度>=3的token才算相关——见模块docstring"文本相关性校验"一节
    的真实误判案例。
    """
    if candidate_name in query or query in candidate_name:
        return True

    query_tokens = {t for t in jieba.cut(query) if len(t) >= 3} - _GENERIC_STOPWORDS
    candidate_tokens = {t for t in jieba.cut(candidate_name) if len(t) >= 3} - _GENERIC_STOPWORDS
    return bool(query_tokens & candidate_tokens)


def search_poi_photo_url(poi_name: str) -> Optional[str]:
    """调高德POI搜索接口，返回相关度最高的候选（高德按相关度排序，取首位）的第一张
    照片URL。

    poi_name预期是完整POI名称（如"张掖·七彩丹霞景区"），不带city参数——见模块
    docstring"演变过程"：city限定这个维度做不到景点内部子设施级别的精度，完整景区名
    本身作为搜索词已经足够具体，不依赖city也能拿到准确候选。

    不要求候选唯一（完整景区名搜索天然会带出很多同名周边结果，如停车场/酒店/讲解
    服务，"只有一个候选"这条规则在这个场景下会导致几乎全部落空）；真正的误配防线是
    _is_relevant_match对首位候选的相关性校验。

    未配置AMAP_GEOCODE_API_KEY、查询无结果、返回格式异常、网络请求失败、或候选
    跟查询词语义不相关时均返回None（不抛出异常）。
    """
    if not AMAP_GEOCODE_API_KEY:
        return None

    params = {
        "key": AMAP_GEOCODE_API_KEY,
        "keywords": poi_name,
        "extensions": "all",
        "output": "json",
    }

    _throttle()
    try:
        resp = requests.get(_SEARCH_URL, params=params, timeout=10)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    if data.get("status") != "1":
        return None
    pois = data.get("pois") or []
    if not pois:
        return None

    candidate_name = pois[0].get("name", "")
    if not _is_relevant_match(poi_name, candidate_name):
        return None

    photos = pois[0].get("photos") or []
    if not photos:
        return None

    url = photos[0].get("url", "")
    return url or None


def _download_as_base64(url: str) -> Optional[tuple[str, str]]:
    """下载图片，返回(base64编码字符串, mime类型)。失败（含网络错误、解码失败、
    宽度低于_MIN_PHOTO_WIDTH_PX）返回None——跟网络失败走同一条"优雅降级"路径，
    调用方保留原有占位框，不阻塞整个网站生成。"""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    try:
        width, _ = Image.open(io.BytesIO(resp.content)).size
    except (UnidentifiedImageError, OSError):
        return None
    if width < _MIN_PHOTO_WIDTH_PX:
        return None

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"

    encoded = base64.b64encode(resp.content).decode("ascii")
    return encoded, content_type


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_cached_or_poi_overview_photo(topic: str, poi_name: str) -> Optional[str]:
    """先查全局缓存，未命中则搜索照片URL->下载转base64->写回缓存，返回可直接用于
    <img src="...">的data URL字符串（data:image/jpeg;base64,...），用于给整个POI
    详情页（树木页英雄区）配一张代表性照片。

    查询/下载失败不写入缓存，下次运行时会重新尝试（跟driving.py/geocode.py一致）。
    """
    cache_key = f"{topic}|{poi_name}"
    cache = _load_cache()

    cached = cache.get(cache_key)
    if cached:
        return f"data:{cached['mime']};base64,{cached['data']}"

    url = search_poi_photo_url(poi_name)
    if url is None:
        return None

    downloaded = _download_as_base64(url)
    if downloaded is None:
        return None

    encoded, mime = downloaded
    cache[cache_key] = {
        "mime": mime,
        "data": encoded,
        "source_url": url,
        "cached_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
    }
    _save_cache(cache)
    return f"data:{mime};base64,{encoded}"
