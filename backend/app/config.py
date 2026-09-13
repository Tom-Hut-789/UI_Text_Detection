"""全局配置：通过 .env / 环境变量注入，集中管理路径、并发度与 LLM 接入参数。"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# backend/app/config.py -> backend/app -> backend -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8", # .env 里有中文注释，不写死 utf-8 会按系统 GBK 解出乱码
        extra="ignore",
    )

    # ---------- 服务 ----------
    app_name: str = "多语种UI文本格式检测系统"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    # 允许跨域的前端地址，"*" 表示不限制（开发期默认）
    cors_origins: str = "*"

    # ---------- 存储 ----------
    data_dir: Path = PROJECT_ROOT / "data"

    # ---------- LLM 接入（兼容任意 OpenAI 协议网关） ----------
    openai_api_key: str = ""
    openai_base_url: str | None = None
    llm_model: str = "gpt-4o"
    # 单张图片的视觉精度："high" 对 OCR / 截断判定更准，代价是 Token 更高
    llm_image_detail: str = "high"
    # 双图对比：把小语种截图与同行英文基准截图一起发给模型，做视觉几何对照。
    # 截断本质是几何问题（同一控件里英文占 60% 宽度、译文占 118%），只看文本长度会丢掉
    # 最关键的证据。代价是小语种请求的 prompt token 近乎翻倍；网关不支持多图输入、
    # 或想省成本时置 false 即可回退为单图。
    llm_dual_image: bool = True
    # 关闭模型思考。部分推理型模型（实测 deepseek-flash）在文字密集的截图（缅甸语等）
    # 上会陷入推理循环：把 max_tokens 全部耗在 reasoning 上、content 为空，导致该图直接
    # 判定失败——实测 2/9 张必然失败，且加大 max_tokens 无效（8192 照样跑满）。
    # 开启后同一张图 completion 从 8192 降到 361，结果正常。
    # 该参数格式是 DeepSeek 系的（{"thinking": {"type": "disabled"}}），其他网关可能不认识，
    # 故默认关闭，确认自己的网关支持后再打开。
    llm_disable_thinking: bool = False
    # 全局最大并发请求数（SDD 7.2 §2）
    llm_concurrency: int = 5
    llm_timeout: float = 120.0
    llm_max_retries: int = 4
    # 单次回复的 Token 上限。注意：推理型模型（返回 reasoning_tokens 的）也会把
    # 思考过程计入这个额度，取值过小会导致还没输出正文就被截断。
    llm_max_tokens: int = 2048
    temperature: float = 0.0

    # ---------- 图片预处理 ----------
    # 长边上限（像素）。Excel 内嵌截图为超长图，原图直传既贵又易超限。
    image_max_side: int = 2048
    # 单张图片 Base64 后的字节上限，超出则继续降采样
    image_max_bytes: int = 1_500_000

    # ---------- Mock 模式 ----------
    # 置 true 时不调用真实模型，返回构造结果，用于打通全链路 / 压测前端
    mock_llm: bool = False

    @field_validator("openai_base_url")
    @classmethod
    def _strip_endpoint_suffix(cls, value: str | None) -> str | None:
        """容忍把**完整端点**填进 BASE_URL。

        OpenAI SDK 会自己在 base_url 后面拼 `/chat/completions`，所以把
        `https://host/v1/chat/completions` 填进来会请求到
        `.../chat/completions/chat/completions` 并返回 404——报错发生在运行时，
        看起来像网关故障或模型不存在，极难定位。这里统一把后缀剥掉。
        """
        if not value:
            return value
        cleaned = value.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if cleaned.endswith(suffix):
                logger.warning(
                    "OPENAI_BASE_URL 填成了完整端点 %s，已自动去掉 %s 后缀；"
                    "SDK 会自行拼接，请只填到 /v1 为止",
                    value, suffix,
                )
                cleaned = cleaned[: -len(suffix)]
                break
        return cleaned.rstrip("/") or None

    @property
    def llm_configured(self) -> bool:
        """是否真正配置了可用的密钥。

        从 .env.example 复制出来的 `.env` 里是形如 `sk-xxxx…` 的占位符，
        直接判空会误认为「已配置」，导致上传后才在运行时失败。
        """
        key = (self.openai_api_key or "").strip()
        if not key or "xxxx" in key.lower():
            return False
        return True

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True)

# FastAPI 依赖系统调用`get_settings()`，依靠缓存保证永远拿到同一个实例。
@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
