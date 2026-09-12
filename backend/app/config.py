"""全局配置：通过 .env / 环境变量注入，集中管理路径、并发度与 LLM 接入参数。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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
