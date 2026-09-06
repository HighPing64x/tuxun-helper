"""ai_client.py — 统一的视觉大模型调用封装。

支持两类后端，按顺序自动检测：

1. OpenAI 兼容接口（国内可直连，推荐）：
   智谱 GLM-4V 系列 bigmodel.cn、阿里通义 Qwen-VL（DashScope 兼容模式）、
   SiliconFlow、DeepSeek、OpenAI 官方、各类中转站等。
   需要：OPENAI_BASE_URL + OPENAI_API_KEY (+ OPENAI_MODEL)

2. Google Gemini（官方 SDK，国内通常需要代理）：
   需要：API_KEY 或 GEMINI_API_KEY

图片统一以 JPEG 字节流传入。OpenAI 兼容路径用 base64 data URL；
Gemini 路径使用官方 SDK 的 Part.from_bytes，不经过磁盘。
"""

from __future__ import annotations

import base64
import logging
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger("tuxun.ai")


class AIError(Exception):
    """AI 调用失败。"""


class AIBackend:
    """解析配置并执行视觉分析调用。"""

    def __init__(
        self,
        provider: str = "auto",
        gemini_api_key: str = "",
        gemini_model: str = "gemini-2.5-flash",
        openai_base_url: str = "",
        openai_api_key: str = "",
        openai_model: str = "",
        timeout: int = 120,
    ):
        self.timeout = timeout
        detected = self._detect(provider, gemini_api_key, openai_base_url, openai_api_key)
        self.provider, self.gemini_api_key, self.openai_base_url, self.openai_api_key = detected
        self.gemini_model = gemini_model
        self.openai_model = openai_model
        self._gemini_client = None

    # ------------------------------------------------------------------
    @staticmethod
    def _detect(provider, gemini_key, openai_base_url, openai_key):
        provider = (provider or "auto").strip().lower()
        if provider == "openai" or provider == "compatible":
            if not (openai_base_url and openai_key):
                raise AIError(
                    "已指定 AI_PROVIDER=openai，但缺少 OPENAI_BASE_URL 或 OPENAI_API_KEY。"
                )
            return "openai", "", openai_base_url.strip().rstrip("/"), openai_key.strip()
        if provider == "gemini":
            if not gemini_key:
                raise AIError("已指定 AI_PROVIDER=gemini，但缺少 API_KEY / GEMINI_API_KEY。")
            return "gemini", gemini_key.strip(), "", ""
        # auto：优先国内可直连的 OpenAI 兼容接口
        if openai_base_url and openai_key:
            return "openai", "", openai_base_url.strip().rstrip("/"), openai_key.strip()
        if gemini_key:
            return "gemini", gemini_key.strip(), "", ""
        raise AIError(
            "未配置任何 AI 后端。请在 .env 中配置其中一组：\n"
            "  国内直连（推荐）: OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL\n"
            "  Google Gemini  : API_KEY（可能需要 PROXY_URL 代理）"
        )

    # ------------------------------------------------------------------
    @property
    def model_name(self) -> str:
        return self.openai_model or self.gemini_model

    def describe(self) -> str:
        if self.provider == "openai":
            return f"OpenAI兼容接口 {self.openai_base_url} (模型: {self.model_name})"
        return f"Google Gemini (模型: {self.gemini_model})"

    # ------------------------------------------------------------------
    def analyze(self, prompt: str, images: List[Tuple[str, bytes]]) -> str:
        """images: [(方向标签, JPEG字节), ...]，返回模型文本输出。"""
        if not images:
            raise AIError("没有可供分析的图片。")
        if self.provider == "openai":
            return self._call_openai(prompt, images)
        return self._call_gemini(prompt, images)

    # ------------------------------------------------------------------
    def _call_openai(self, prompt: str, images: List[Tuple[str, bytes]]) -> str:
        content = [{"type": "text", "text": prompt}]
        for label, image_bytes in images:
            if label:
                content.append({"type": "text", "text": f"【{label}视图】"})
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            )
        payload = {
            "model": self.openai_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
        }
        try:
            resp = requests.post(
                f"{self.openai_base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.openai_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                detail = resp.text[:300]
                raise AIError(
                    f"模型接口返回 HTTP {resp.status_code}：{detail}\n"
                    f"（请检查 OPENAI_MODEL 是否支持图片输入、Key 是否有效、账户余额是否充足）"
                )
            data = resp.json()
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if not text:
                raise AIError(f"模型返回了空内容：{str(data)[:300]}")
            return text
        except AIError:
            raise
        except requests.exceptions.RequestException as exc:
            raise AIError(f"请求模型接口失败：{exc}") from exc

    # ------------------------------------------------------------------
    def _get_gemini_client(self):
        if self._gemini_client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise AIError(
                    "未安装 google-genai 库。若使用国内直连模型可忽略本项；"
                    "若要使用 Gemini，请执行: pip install google-genai"
                ) from exc
            self._gemini_client = genai.Client(api_key=self.gemini_api_key)
        return self._gemini_client

    def _call_gemini(self, prompt: str, images: List[Tuple[str, bytes]]) -> str:
        from google.genai import types

        client = self._get_gemini_client()
        contents: list = [prompt]
        for label, image_bytes in images:
            if label:
                contents.append(f"--- {label}视图 ---")
            contents.append(
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
            )
        try:
            response = client.models.generate_content(
                model=self.gemini_model, contents=contents
            )
        except Exception as exc:
            raise AIError(
                f"Gemini 调用失败：{exc}\n（国内网络通常需要设置 PROXY_URL 代理后重试）"
            ) from exc
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise AIError("Gemini 返回了空内容（可能被安全策略拦截）。")
        return text


def build_backend_from_env(env) -> AIBackend:
    """从环境变量字典（os.environ 或 dotenv 解析结果）构建 AIBackend。"""
    gemini_key = env.get("API_KEY") or env.get("GEMINI_API_KEY") or ""
    return AIBackend(
        provider=env.get("AI_PROVIDER", "auto"),
        gemini_api_key=gemini_key,
        gemini_model=env.get("GEMINI_MODEL", "gemini-2.5-flash"),
        openai_base_url=env.get("OPENAI_BASE_URL", ""),
        openai_api_key=env.get("OPENAI_API_KEY", ""),
        openai_model=env.get("OPENAI_MODEL", ""),
        timeout=int(env.get("AI_TIMEOUT", "120") or 120),
    )
