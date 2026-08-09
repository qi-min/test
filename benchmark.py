from __future__ import annotations

import argparse
import configparser
import csv
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox


STORAGE_CHOICES = ("none", "oss", "file")
DEFAULT_PROVIDER = "aliyun"
DEFAULT_MAX_WORKERS = 1000
DEFAULT_PRODUCT_NAMES = {
    "aliyun": "阿里云",
    "vol": "字节",
    "byte": "字节",
    "ags": "AGS",
}
PRINT_LOCK = threading.Lock()


# Check type constants
CHECK_TYPE_COMMAND = "command"
CHECK_TYPE_REST = "rest"
REST_CHECK_TIMEOUT_S = 60.0       # 1 min overall timeout for REST check
REST_CHECK_POLL_INTERVAL_S = 0.01  # 10ms poll interval


@dataclass(frozen=True)
class E2BSettings:
    driver: str
    api_key: str
    api_url: str
    domain: str
    template: str
    sandbox_timeout: int
    vpc_config: dict | None
    oss_config: dict | None
    role_arn: str
    file_metadata_key: str
    file_config: dict | None
    extra_metadata: dict[str, str]
    check_type: str = CHECK_TYPE_COMMAND  # "command" or "rest"
    rest_port: int = 0
    rest_path: str = "/healthz"


@dataclass(frozen=True)
class VolcengineNativeSettings:
    driver: str
    access_key: str
    secret_key: str
    region: str
    function_id: str
    sandbox_lifetime: int
    sandbox_lifetime_unit: str
    ready_timeout_seconds: float
    poll_interval_seconds: float
    sdk_auto_retry: bool
    endpoint: str


Settings = E2BSettings | VolcengineNativeSettings


@dataclass
class TrialResult:
    provider: str
    product_name: str
    driver: str
    storage: str
    target_rate_per_s: int
    duration_seconds: float
    configured_max_workers: int
    effective_max_workers: int
    trial_index: int
    scheduled_at_utc: str
    queue_delay_ms: float
    api_latency_ms: float | None
    first_command_latency_ms: float | None
    second_command_latency_ms: float | None
    api_success: bool
    ready_success: bool
    cleanup_success: bool
    sandbox_id: str
    failure_phase: str
    error_type: str
    error_message: str
    error_traceback: str
    cleanup_error_type: str
    cleanup_error_message: str
    cleanup_error_traceback: str
    ready_poll_count: int
    last_observed_status: str


@dataclass(frozen=True)
class ConfiguredRun:
    provider: str
    product_name: str
    rates: list[int]
    duration_seconds: float
    rate_durations: list[float]      # per-rate duration; same length as rates
    rate_interval_seconds: float     # pause (s) between each rate; applied uniformly
    storages: list[str]
    max_workers: int
    output: Path
    exclude_first_from_mean: bool
    settings: Settings


def env_json(name: str) -> dict | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return parse_json_object(raw, name)


def parse_json_object(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} 不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 必须是 JSON 对象")
    return value


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def load_settings(require_credentials: bool = True) -> E2BSettings:
    load_dotenv()
    extra = env_json("E2B_EXTRA_METADATA_JSON") or {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
        raise RuntimeError("E2B_EXTRA_METADATA_JSON 的键和值都必须是字符串")

    return E2BSettings(
        driver="e2b",
        api_key=require_env("E2B_API_KEY") if require_credentials else "",
        api_url=require_env("E2B_API_URL") if require_credentials else "",
        domain=require_env("E2B_DOMAIN") if require_credentials else "",
        template=os.environ.get("E2B_TEMPLATE", "code-interpreter-v1").strip()
        or "code-interpreter-v1",
        sandbox_timeout=int(os.environ.get("E2B_SANDBOX_TIMEOUT", "60")),
        vpc_config=env_json("E2B_VPC_CONFIG_JSON"),
        oss_config=env_json("E2B_OSS_CONFIG_JSON"),
        role_arn=os.environ.get("E2B_ROLE_ARN", "").strip(),
        file_metadata_key=os.environ.get("E2B_FILE_METADATA_KEY", "").strip(),
        file_config=env_json("E2B_FILE_CONFIG_JSON"),
        extra_metadata=extra,
    )


def config_json(
    section: configparser.SectionProxy,
    option: str,
) -> dict | None:
    raw = section.get(option, "").strip()
    return parse_json_object(raw, option) if raw else None


def required_config_value(
    section: configparser.SectionProxy,
    option: str,
) -> str:
    value = section.get(option, "").strip()
    if not value:
        raise RuntimeError(f"配置段 [{section.name}] 缺少 {option}")
    return value


def config_secret(
    section: configparser.SectionProxy,
    option: str,
    env_option: str,
) -> str:
    value = section.get(option, "").strip()
    env_name = section.get(env_option, "").strip()
    if not value and env_name:
        value = os.environ.get(env_name, "").strip()
    if not value or value.upper().startswith(("REPLACE_", "YOUR_")):
        source = f" 或环境变量 {env_name}" if env_name else ""
        raise RuntimeError(f"[{section.name}] 缺少有效的 {option}{source}")
    return value


def load_configured_run(path: Path) -> ConfiguredRun:
    load_dotenv()
    if not path.exists():
        raise RuntimeError(
            f"找不到配置文件: {path}；请先复制 benchmark.template.ini 为 benchmark.ini"
        )

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (configparser.Error, OSError) as exc:
        raise RuntimeError(f"读取配置文件失败: {exc}") from exc

    if "run" not in parser:
        raise RuntimeError("配置文件缺少 [run] 段")
    run_section = parser["run"]
    try:
        provider = parse_provider(required_config_value(run_section, "product"))
        rates = parse_csv_ints(required_config_value(run_section, "rates"))
        storages = parse_storages(run_section.get("storages", "none"))
    except argparse.ArgumentTypeError as exc:
        raise RuntimeError(f"配置文件参数错误: {exc}") from exc

    profile_name = f"product.{provider}"
    if profile_name not in parser:
        raise RuntimeError(f"配置文件缺少 [{profile_name}] 产品段")
    product = parser[profile_name]
    product_name = product.get("name", provider).strip() or provider

    duration_seconds = run_section.getfloat("duration_seconds", fallback=60.0)
    max_workers = run_section.getint("max_workers", fallback=DEFAULT_MAX_WORKERS)
    exclude_first = run_section.getboolean(
        "exclude_first_from_mean",
        fallback=True,
    )
    if duration_seconds <= 0:
        raise RuntimeError("[run] duration_seconds 必须大于 0")
    if max_workers <= 0:
        raise RuntimeError("[run] max_workers 必须大于 0")

    # Parse optional per-rate durations (rate_durations = 5,10,30)
    raw_rate_durations = run_section.get("rate_durations", "").strip()
    if raw_rate_durations:
        try:
            rate_durations = [
                float(item.strip())
                for item in raw_rate_durations.split(",")
                if item.strip()
            ]
        except ValueError as exc:
            raise RuntimeError(
                "[run] rate_durations 必须是逗号分隔的正数"
            ) from exc
        if len(rate_durations) != len(rates):
            raise RuntimeError(
                f"[run] rate_durations 的数量 ({len(rate_durations)}) "
                f"必须与 rates 的数量 ({len(rates)}) 相同"
            )
        if any(d <= 0 for d in rate_durations):
            raise RuntimeError("[run] rate_durations 中每个值必须大于 0")
    else:
        rate_durations = [duration_seconds] * len(rates)

    # Parse optional uniform interval between rates (rate_interval_seconds = 30)
    rate_interval_seconds = run_section.getfloat("rate_interval_seconds", fallback=0.0)
    if rate_interval_seconds < 0:
        raise RuntimeError("[run] rate_interval_seconds 必须 >= 0")

    driver = product.get("driver", "e2b").strip().lower() or "e2b"
    if driver == "e2b":
        extra_metadata = config_json(product, "extra_metadata_json") or {}
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in extra_metadata.items()
        ):
            raise RuntimeError(
                f"[{profile_name}] extra_metadata_json 的键和值必须是字符串"
            )
        raw_check_type = product.get("check_type", CHECK_TYPE_COMMAND).strip().lower()
        if raw_check_type not in (CHECK_TYPE_COMMAND, CHECK_TYPE_REST):
            raise RuntimeError(
                f"[{profile_name}] check_type 只支持 '{CHECK_TYPE_COMMAND}' 或 '{CHECK_TYPE_REST}'"
            )
        raw_rest_port = product.get("rest_port", "0").strip()
        try:
            rest_port = int(raw_rest_port) if raw_rest_port else 0
        except ValueError as exc:
            raise RuntimeError(
                f"[{profile_name}] rest_port 必须是整数"
            ) from exc
        if raw_check_type == CHECK_TYPE_REST and rest_port <= 0:
            raise RuntimeError(
                f"[{profile_name}] check_type=rest 时必须配置有效的 rest_port（正整数）"
            )
        rest_path = product.get("rest_path", "/healthz").strip() or "/healthz"

        settings: Settings = E2BSettings(
            driver=driver,
            api_key=config_secret(product, "api_key", "api_key_env"),
            api_url=required_config_value(product, "api_url"),
            domain=required_config_value(product, "domain"),
            template=required_config_value(product, "template"),
            sandbox_timeout=product.getint("sandbox_timeout", fallback=300),
            vpc_config=config_json(product, "vpc_config_json"),
            oss_config=config_json(product, "oss_config_json"),
            role_arn=product.get("role_arn", "").strip(),
            file_metadata_key=product.get("file_metadata_key", "").strip(),
            file_config=config_json(product, "file_config_json"),
            extra_metadata=extra_metadata,
            check_type=raw_check_type,
            rest_port=rest_port,
            rest_path=rest_path,
        )
    elif driver == "volcengine_native":
        settings = VolcengineNativeSettings(
            driver=driver,
            access_key=config_secret(product, "access_key", "access_key_env"),
            secret_key=config_secret(product, "secret_key", "secret_key_env"),
            region=required_config_value(product, "region"),
            function_id=required_config_value(product, "function_id"),
            sandbox_lifetime=product.getint("sandbox_lifetime", fallback=6),
            sandbox_lifetime_unit=(
                product.get("sandbox_lifetime_unit", "minute").strip() or "minute"
            ),
            ready_timeout_seconds=product.getfloat(
                "ready_timeout_seconds", fallback=120.0
            ),
            poll_interval_seconds=product.getfloat(
                "poll_interval_seconds", fallback=0.1
            ),
            sdk_auto_retry=product.getboolean("sdk_auto_retry", fallback=False),
            endpoint=product.get("endpoint", "").strip(),
        )
        if settings.sandbox_lifetime <= 0:
            raise RuntimeError(f"[{profile_name}] sandbox_lifetime 必须大于 0")
        if settings.ready_timeout_seconds <= 0:
            raise RuntimeError(f"[{profile_name}] ready_timeout_seconds 必须大于 0")
        if settings.poll_interval_seconds <= 0:
            raise RuntimeError(f"[{profile_name}] poll_interval_seconds 必须大于 0")
    else:
        raise RuntimeError(
            f"[{profile_name}] driver 只支持 e2b 或 volcengine_native"
        )
    return ConfiguredRun(
        provider=provider,
        product_name=product_name,
        rates=rates,
        duration_seconds=duration_seconds,
        rate_durations=rate_durations,
        rate_interval_seconds=rate_interval_seconds,
        storages=storages,
        max_workers=max_workers,
        output=Path(run_section.get("output", "results").strip() or "results"),
        exclude_first_from_mean=exclude_first,
        settings=settings,
    )


class VolcengineNativeRuntime:
    def __init__(self, settings: VolcengineNativeSettings, max_workers: int):
        try:
            import volcenginesdkcore
            import volcenginesdkvefaas
        except ImportError as exc:
            raise RuntimeError(
                "火山原生模式缺少 volcengine-python-sdk；请先执行 "
                "python -m pip install -r requirements.txt"
            ) from exc

        configuration = volcenginesdkcore.Configuration()
        configuration.ak = settings.access_key
        configuration.sk = settings.secret_key
        configuration.region = settings.region
        configuration.auto_retry = settings.sdk_auto_retry
        configuration.connection_pool_maxsize = max(1, max_workers)
        if settings.endpoint:
            configuration.host = settings.endpoint

        self.sdk = volcenginesdkvefaas
        self.api_client = volcenginesdkcore.ApiClient(configuration)
        self.api = volcenginesdkvefaas.VEFAASApi(self.api_client)

    def create(self, settings: VolcengineNativeSettings) -> Any:
        request = self.sdk.CreateSandboxRequest(
            function_id=settings.function_id,
            timeout=settings.sandbox_lifetime,
            timeout_unit=settings.sandbox_lifetime_unit,
        )
        return self.api.create_sandbox(request)

    def describe(self, settings: VolcengineNativeSettings, sandbox_id: str) -> Any:
        request = self.sdk.DescribeSandboxRequest(
            function_id=settings.function_id,
            sandbox_id=sandbox_id,
        )
        return self.api.describe_sandbox(request)

    def kill(self, settings: VolcengineNativeSettings, sandbox_id: str) -> None:
        request = self.sdk.KillSandboxRequest(
            function_id=settings.function_id,
            sandbox_id=sandbox_id,
        )
        self.api.kill_sandbox(request)

    def close(self) -> None:
        close = getattr(self.api_client, "close", None)
        if callable(close):
            close()


def metadata_for(storage: str, settings: Settings) -> dict[str, str]:
    if isinstance(settings, VolcengineNativeSettings):
        if storage != "none":
            raise RuntimeError("火山原生 SDK 当前只支持 storages = none")
        return {}

    metadata = dict(settings.extra_metadata)
    if settings.vpc_config:
        metadata["fc.sandbox.network.vpc"] = json.dumps(
            settings.vpc_config, separators=(",", ":")
        )

    if storage == "oss":
        if not settings.oss_config:
            raise RuntimeError("OSS 场景缺少 E2B_OSS_CONFIG_JSON")
        if not settings.role_arn:
            raise RuntimeError("OSS 场景缺少 E2B_ROLE_ARN")
        metadata["fc.sandbox.storage.oss"] = json.dumps(
            settings.oss_config, separators=(",", ":")
        )
        metadata["fc.sandbox.auth.role"] = settings.role_arn
    elif storage == "file":
        if not settings.file_metadata_key or not settings.file_config:
            raise RuntimeError(
                "文件存储场景缺少 E2B_FILE_METADATA_KEY 或 E2B_FILE_CONFIG_JSON"
            )
        metadata[settings.file_metadata_key] = json.dumps(
            settings.file_config, separators=(",", ":")
        )
    elif storage != "none":
        raise ValueError(f"未知存储类型: {storage}")
    return metadata


def one_trial_volcengine_native(
    *,
    settings: VolcengineNativeSettings,
    runtime: VolcengineNativeRuntime,
    provider: str,
    product_name: str,
    storage: str,
    rate: int,
    duration_seconds: float,
    configured_max_workers: int,
    effective_max_workers: int,
    trial_index: int,
    scheduled_monotonic: float,
    scheduled_at_utc: str,
) -> TrialResult:
    sandbox_id = ""
    api_latency_ms = None
    first_command_latency_ms = None
    second_command_latency_ms = None
    api_success = False
    ready_success = False
    cleanup_success = True
    failure_phase = ""
    error_type = ""
    error_message = ""
    error_traceback = ""
    cleanup_error_type = ""
    cleanup_error_message = ""
    cleanup_error_traceback = ""
    ready_poll_count = 0
    last_observed_status = ""

    actual_start = time.perf_counter()
    queue_delay_ms = max(0.0, (actual_start - scheduled_monotonic) * 1000)
    create_start = time.perf_counter()
    current_phase = "api_create"
    last_describe_error = ""

    try:
        create_response = runtime.create(settings)
        api_return = time.perf_counter()
        api_latency_ms = (api_return - create_start) * 1000
        sandbox_id = str(
            getattr(
                create_response,
                "sandbox_id",
                getattr(create_response, "id", ""),
            )
            or ""
        )
        if not sandbox_id:
            raise RuntimeError("CreateSandbox 返回成功但没有 Sandbox ID")
        api_success = True

        current_phase = "ready_poll"
        deadline = create_start + settings.ready_timeout_seconds
        while time.perf_counter() <= deadline:
            try:
                describe_response = runtime.describe(settings, sandbox_id)
                ready_poll_count += 1
                status = getattr(
                    describe_response,
                    "status",
                    getattr(describe_response, "state", None),
                )
                last_observed_status = str(status or "")
                normalized_status = last_observed_status.strip().lower()
            except Exception as exc:
                ready_poll_count += 1
                last_describe_error = (
                    f"{type(exc).__name__}: {str(exc)}"
                    .replace("\r", " ")
                    .replace("\n", " ")[:1000]
                )
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                time.sleep(min(settings.poll_interval_seconds, remaining))
                continue

            if normalized_status == "ready":
                first_ready = time.perf_counter()
                first_command_latency_ms = (first_ready - create_start) * 1000

                current_phase = "ready_confirm"
                confirm_response = runtime.describe(settings, sandbox_id)
                ready_poll_count += 1
                confirm_status = getattr(
                    confirm_response,
                    "status",
                    getattr(confirm_response, "state", None),
                )
                last_observed_status = str(confirm_status or "")
                second_ready = time.perf_counter()
                second_command_latency_ms = (second_ready - create_start) * 1000
                if last_observed_status.strip().lower() == "ready":
                    ready_success = True
                else:
                    failure_phase = "ready_confirm"
                    error_type = "ReadyConfirmationFailed"
                    error_message = (
                        "首次查询为 Ready，但第二次查询状态为 "
                        f"{last_observed_status or '(空)'}"
                    )
                break

            if normalized_status in {"failed", "error"}:
                failure_phase = "ready_poll"
                error_type = "SandboxTerminalState"
                remote_code = str(getattr(describe_response, "error_code", "") or "")
                remote_message = str(
                    getattr(describe_response, "error_message", "") or ""
                )
                error_message = (
                    f"Sandbox 进入终止状态 {last_observed_status}; "
                    f"error_code={remote_code or '(空)'}; "
                    f"error_message={remote_message or '(空)'}"
                )[:1000]
                break

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(settings.poll_interval_seconds, remaining))

        if not ready_success and not error_type:
            failure_phase = "ready_poll"
            error_type = "ReadyTimeout"
            error_message = (
                f"从 CreateSandbox 开始超过 {settings.ready_timeout_seconds:g} 秒"
                f"仍未 Ready；最后状态={last_observed_status or '(空)'}"
            )
            if last_describe_error:
                error_message += f"；最后查询异常={last_describe_error}"
            error_message = error_message[:1000]
    except Exception as exc:
        failure_phase = current_phase
        error_type = type(exc).__name__
        error_message = str(exc).replace("\r", " ").replace("\n", " ")[:1000]
        error_traceback = traceback.format_exc()

    return TrialResult(
        provider=provider,
        product_name=product_name,
        driver=settings.driver,
        storage=storage,
        target_rate_per_s=rate,
        duration_seconds=(
            int(duration_seconds)
            if float(duration_seconds).is_integer()
            else duration_seconds
        ),
        configured_max_workers=configured_max_workers,
        effective_max_workers=effective_max_workers,
        trial_index=trial_index,
        scheduled_at_utc=scheduled_at_utc,
        queue_delay_ms=round(queue_delay_ms, 3),
        api_latency_ms=round(api_latency_ms, 3) if api_latency_ms is not None else None,
        first_command_latency_ms=(
            round(first_command_latency_ms, 3)
            if first_command_latency_ms is not None
            else None
        ),
        second_command_latency_ms=(
            round(second_command_latency_ms, 3)
            if second_command_latency_ms is not None
            else None
        ),
        api_success=api_success,
        ready_success=ready_success,
        cleanup_success=cleanup_success,
        sandbox_id=sandbox_id,
        failure_phase=failure_phase,
        error_type=error_type,
        error_message=error_message,
        error_traceback=error_traceback,
        cleanup_error_type=cleanup_error_type,
        cleanup_error_message=cleanup_error_message,
        cleanup_error_traceback=cleanup_error_traceback,
        ready_poll_count=ready_poll_count,
        last_observed_status=last_observed_status,
    )


def one_trial(
    *,
    settings: Settings,
    provider: str,
    product_name: str,
    storage: str,
    rate: int,
    duration_seconds: float,
    configured_max_workers: int,
    effective_max_workers: int,
    trial_index: int,
    scheduled_monotonic: float,
    scheduled_at_utc: str,
    native_runtime: VolcengineNativeRuntime | None = None,
) -> tuple[TrialResult, Sandbox | None]:
    if isinstance(settings, VolcengineNativeSettings):
        if native_runtime is None:
            raise RuntimeError("火山原生模式缺少已初始化的 SDK 客户端")
        return one_trial_volcengine_native(
            settings=settings,
            runtime=native_runtime,
            provider=provider,
            product_name=product_name,
            storage=storage,
            rate=rate,
            duration_seconds=duration_seconds,
            configured_max_workers=configured_max_workers,
            effective_max_workers=effective_max_workers,
            trial_index=trial_index,
            scheduled_monotonic=scheduled_monotonic,
            scheduled_at_utc=scheduled_at_utc,
        ), None

    sandbox = None
    cleanup_success = True
    sandbox_id = ""
    api_latency_ms = None
    first_command_latency_ms = None
    second_command_latency_ms = None
    api_success = False
    ready_success = False
    failure_phase = ""
    error_type = ""
    error_message = ""
    error_traceback = ""
    cleanup_error_type = ""
    cleanup_error_message = ""
    cleanup_error_traceback = ""

    actual_start = time.perf_counter()
    queue_delay_ms = max(0.0, (actual_start - scheduled_monotonic) * 1000)

    current_phase = "api_create"
    try:
        create_start = time.perf_counter()
        sandbox = Sandbox.create(
            template=settings.template,
            timeout=settings.sandbox_timeout,
            api_key=settings.api_key,
            api_url=settings.api_url,
            domain=settings.domain,
            metadata=metadata_for(storage, settings),
        )
        api_return = time.perf_counter()
        api_latency_ms = (api_return - create_start) * 1000
        api_success = True
        sandbox_id = str(
            getattr(sandbox, "sandbox_id", getattr(sandbox, "id", ""))
        )

        if settings.check_type == CHECK_TYPE_REST:
            # --- REST check mode ---
            # Poll the REST endpoint until it returns HTTP 200, with 10ms interval
            # and 1-minute overall timeout.
            current_phase = "first_command"
            host = sandbox.get_host(settings.rest_port)
            rest_path = settings.rest_path
            if not rest_path.startswith("/"):
                rest_path = "/" + rest_path
            url = f"https://{host}{rest_path}"
            access_token = getattr(sandbox, "traffic_access_token", None) or ""
            headers = {}
            if access_token:
                headers["e2b-traffic-access-token"] = access_token

            rest_deadline = create_start + REST_CHECK_TIMEOUT_S
            rest_poll_count = 0
            first_rest_ok = False
            last_status_code: int | None = None
            while True:
                try:
                    response = requests.get(url, headers=headers, timeout=10)
                    last_status_code = response.status_code
                    rest_poll_count += 1
                    if response.status_code == 200:
                        first_rest_ok = True
                        break
                except Exception:
                    rest_poll_count += 1

                remaining = rest_deadline - time.perf_counter()
                if remaining <= 0:
                    break
                time.sleep(min(REST_CHECK_POLL_INTERVAL_S, remaining))

            first_command_ready = time.perf_counter()
            first_command_latency_ms = (first_command_ready - create_start) * 1000

            if not first_rest_ok:
                failure_phase = "first_command"
                error_type = "RestCheckTimeout"
                error_message = (
                    f"REST 检查超过 {REST_CHECK_TIMEOUT_S:g} 秒仍未返回 200；"
                    f"最后状态码={last_status_code or '(无响应)'}；"
                    f"url={url}"
                )[:1000]
            else:
                # Second REST check (confirm)
                current_phase = "second_command"
                second_rest_ok = False
                last_status_code2: int | None = None
                rest_deadline2 = time.perf_counter() + REST_CHECK_TIMEOUT_S
                while True:
                    try:
                        response2 = requests.get(url, headers=headers, timeout=10)
                        last_status_code2 = response2.status_code
                        if response2.status_code == 200:
                            second_rest_ok = True
                            break
                    except Exception:
                        pass

                    remaining2 = rest_deadline2 - time.perf_counter()
                    if remaining2 <= 0:
                        break
                    time.sleep(min(REST_CHECK_POLL_INTERVAL_S, remaining2))

                second_command_ready = time.perf_counter()
                second_command_latency_ms = (second_command_ready - create_start) * 1000
                ready_success = second_rest_ok
                if not ready_success:
                    failure_phase = "second_command"
                    error_type = "RestCheckSecondTimeout"
                    error_message = (
                        f"第二次 REST 检查超过 {REST_CHECK_TIMEOUT_S:g} 秒仍未返回 200；"
                        f"最后状态码={last_status_code2 or '(无响应)'}；"
                        f"url={url}"
                    )[:1000]
        else:
            # --- command check mode (default) ---
            current_phase = "first_command"
            first_result = sandbox.commands.run(
                "python3 -c \"print('SANDBOX_FIRST_COMMAND')\"",
                timeout=30,
            )
            first_command_ready = time.perf_counter()
            first_command_latency_ms = (first_command_ready - create_start) * 1000
            first_stdout = getattr(first_result, "stdout", "") or ""
            first_command_success = "SANDBOX_FIRST_COMMAND" in first_stdout
            if not first_command_success:
                failure_phase = "first_command"
                error_type = "FirstCommandCheckFailed"
                error_message = "首条命令未返回预期标记"
            else:
                current_phase = "second_command"
                second_result = sandbox.commands.run(
                    "python3 -c \"print('SANDBOX_SECOND_COMMAND')\"",
                    timeout=30,
                )
                second_command_ready = time.perf_counter()
                second_command_latency_ms = (second_command_ready - create_start) * 1000
                second_stdout = getattr(second_result, "stdout", "") or ""
                ready_success = "SANDBOX_SECOND_COMMAND" in second_stdout
                if not ready_success:
                    failure_phase = "second_command"
                    error_type = "SecondCommandCheckFailed"
                    error_message = "第二条命令未返回预期标记"
    except Exception as exc:  # 保留单次失败，不中断整轮测试。
        failure_phase = current_phase
        error_type = type(exc).__name__
        error_message = str(exc).replace("\r", " ").replace("\n", " ")[:1000]
        error_traceback = traceback.format_exc()

    return TrialResult(
        provider=provider,
        product_name=product_name,
        driver=settings.driver,
        storage=storage,
        target_rate_per_s=rate,
        duration_seconds=(
            int(duration_seconds)
            if float(duration_seconds).is_integer()
            else duration_seconds
        ),
        configured_max_workers=configured_max_workers,
        effective_max_workers=effective_max_workers,
        trial_index=trial_index,
        scheduled_at_utc=scheduled_at_utc,
        queue_delay_ms=round(queue_delay_ms, 3),
        api_latency_ms=round(api_latency_ms, 3) if api_latency_ms is not None else None,
        first_command_latency_ms=(
            round(first_command_latency_ms, 3)
            if first_command_latency_ms is not None
            else None
        ),
        second_command_latency_ms=(
            round(second_command_latency_ms, 3)
            if second_command_latency_ms is not None
            else None
        ),
        api_success=api_success,
        ready_success=ready_success,
        cleanup_success=cleanup_success,
        sandbox_id=sandbox_id,
        failure_phase=failure_phase,
        error_type=error_type,
        error_message=error_message,
        error_traceback=error_traceback,
        cleanup_error_type=cleanup_error_type,
        cleanup_error_message=cleanup_error_message,
        cleanup_error_traceback=cleanup_error_traceback,
        ready_poll_count=0,
        last_observed_status="commands_ready" if ready_success else "",
    ), sandbox


def run_rate(
    settings: Settings,
    provider: str,
    product_name: str,
    storage: str,
    rate: int,
    duration_seconds: float,
    max_workers: int,
    native_runtime: VolcengineNativeRuntime | None = None,
) -> list[TrialResult]:
    attempts = max(1, math.ceil(rate * duration_seconds))
    effective_max_workers = min(max_workers, attempts)
    futures: list[Future[tuple[TrialResult, Sandbox | None]]] = []
    run_start = time.perf_counter() + 0.25
    utc_start = datetime.now(timezone.utc).timestamp() + 0.25

    with ThreadPoolExecutor(
        max_workers=effective_max_workers,
        thread_name_prefix="sandbox-create",
    ) as pool:
        for index in range(attempts):
            scheduled = run_start + index / rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            scheduled_utc = datetime.fromtimestamp(
                utc_start + index / rate, tz=timezone.utc
            ).isoformat()
            futures.append(
                pool.submit(
                    one_trial,
                    settings=settings,
                    provider=provider,
                    product_name=product_name,
                    storage=storage,
                    rate=rate,
                    duration_seconds=duration_seconds,
                    configured_max_workers=max_workers,
                    effective_max_workers=effective_max_workers,
                    trial_index=index + 1,
                    scheduled_monotonic=scheduled,
                    scheduled_at_utc=scheduled_utc,
                    native_runtime=native_runtime,
                )
            )

        results: list[TrialResult] = []
        sandboxes: list[Sandbox | None] = []
        completed = 0
        for future in as_completed(futures):
            result, sandbox = future.result()
            results.append(result)
            sandboxes.append(sandbox)
            completed += 1
            if completed == attempts or completed % max(1, attempts // 10) == 0:
                with PRINT_LOCK:
                    print(
                        f"  进度 {completed}/{attempts}",
                        flush=True,
                    )

    # 批量清理：每轮 rate 结束后统一删除所有沙箱
    print(f"  清理 {len(results)} 个沙箱...", flush=True)
    for result, sandbox in zip(results, sandboxes):
        if isinstance(settings, VolcengineNativeSettings):
            if not result.sandbox_id:
                continue
            try:
                if native_runtime is not None:
                    native_runtime.kill(settings, result.sandbox_id)
            except Exception as exc:
                result.cleanup_success = False
                result.cleanup_error_type = type(exc).__name__
                result.cleanup_error_message = (
                    str(exc).replace("\r", " ").replace("\n", " ")[:1000]
                )
                result.cleanup_error_traceback = traceback.format_exc()
        else:
            if sandbox is None:
                continue
            try:
                ok = bool(sandbox.kill())
                if not ok:
                    result.cleanup_success = False
                    result.cleanup_error_type = "CleanupReturnedFalse"
                    result.cleanup_error_message = "sandbox.kill() 返回 False"
            except Exception as exc:
                result.cleanup_success = False
                result.cleanup_error_type = type(exc).__name__
                result.cleanup_error_message = (
                    str(exc).replace("\r", " ").replace("\n", " ")[:1000]
                )
                result.cleanup_error_traceback = traceback.format_exc()

    return sorted(results, key=lambda row: row.trial_index)


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return round(ordered[index], 3)


def summarize(
    results: list[TrialResult],
    exclude_first_from_mean: bool = True,
) -> list[dict]:
    groups: dict[
        tuple[str, str, str, str, int, float, int, int],
        list[TrialResult],
    ] = {}
    for row in results:
        groups.setdefault(
            (
                row.provider,
                row.product_name,
                row.driver,
                row.storage,
                row.target_rate_per_s,
                row.duration_seconds,
                row.configured_max_workers,
                row.effective_max_workers,
            ),
            [],
        ).append(row)

    summary = []
    for (
        provider,
        product_name,
        driver,
        storage,
        rate,
        duration_seconds,
        configured_max_workers,
        effective_max_workers,
    ), rows in sorted(groups.items()):
        api = [row.api_latency_ms for row in rows if row.api_latency_ms is not None]
        command = [
            row.first_command_latency_ms
            for row in rows
            if row.first_command_latency_ms is not None
        ]
        second_command = [
            row.second_command_latency_ms
            for row in rows
            if row.second_command_latency_ms is not None
        ]
        mean_rows = [
            row
            for row in rows
            if not exclude_first_from_mean or row.trial_index != 1
        ]
        api_mean = [
            row.api_latency_ms
            for row in mean_rows
            if row.api_latency_ms is not None
        ]
        command_mean = [
            row.first_command_latency_ms
            for row in mean_rows
            if row.first_command_latency_ms is not None
        ]
        second_command_mean = [
            row.second_command_latency_ms
            for row in mean_rows
            if row.second_command_latency_ms is not None
        ]
        queue = [row.queue_delay_ms for row in rows]
        attempts = len(rows)
        summary.append(
            {
                "provider": provider,
                "product_name": product_name,
                "driver": driver,
                "storage": storage,
                "target_rate_per_s": rate,
                "rate_label": f"{rate}tps",
                "duration_seconds": duration_seconds,
                "configured_max_workers": configured_max_workers,
                "effective_max_workers": effective_max_workers,
                "attempts": attempts,
                "mean_excludes_first_trial": exclude_first_from_mean,
                "api_success_rate_pct": round(
                    100 * sum(row.api_success for row in rows) / attempts, 3
                ),
                "ready_success_rate_pct": round(
                    100 * sum(row.ready_success for row in rows) / attempts, 3
                ),
                "cleanup_success_rate_pct": round(
                    100 * sum(row.cleanup_success for row in rows) / attempts, 3
                ),
                "api_latency_min_ms": round(min(api), 3) if api else None,
                "api_latency_max_ms": round(max(api), 3) if api else None,
                "api_latency_mean_sample_count": len(api_mean),
                "api_latency_mean_ms": round(statistics.fmean(api_mean), 3)
                if api_mean
                else None,
                "api_latency_p50_ms": percentile(api, 50),
                "api_latency_p90_ms": percentile(api, 90),
                "api_latency_p95_ms": percentile(api, 95),
                "api_latency_p99_ms": percentile(api, 99),
                "first_command_latency_min_ms": round(min(command), 3)
                if command
                else None,
                "first_command_latency_max_ms": round(max(command), 3)
                if command
                else None,
                "first_command_latency_mean_sample_count": len(command_mean),
                "first_command_latency_mean_ms": round(
                    statistics.fmean(command_mean), 3
                )
                if command_mean
                else None,
                "first_command_latency_p50_ms": percentile(command, 50),
                "first_command_latency_p90_ms": percentile(command, 90),
                "first_command_latency_p95_ms": percentile(command, 95),
                "first_command_latency_p99_ms": percentile(command, 99),
                "second_command_latency_min_ms": round(min(second_command), 3)
                if second_command
                else None,
                "second_command_latency_max_ms": round(max(second_command), 3)
                if second_command
                else None,
                "second_command_latency_mean_sample_count": len(
                    second_command_mean
                ),
                "second_command_latency_mean_ms": round(
                    statistics.fmean(second_command_mean), 3
                )
                if second_command_mean
                else None,
                "second_command_latency_p50_ms": percentile(second_command, 50),
                "second_command_latency_p90_ms": percentile(second_command, 90),
                "second_command_latency_p95_ms": percentile(second_command, 95),
                "second_command_latency_p99_ms": percentile(second_command, 99),
                "schedule_delay_p95_ms": percentile(queue, 95),
            }
        )
    return summary


def write_csv(
    path: Path,
    rows: list[dict],
    fieldnames: list[str] | None = None,
) -> None:
    if not rows and not fieldnames:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames or list(rows[0]),
        )
        writer.writeheader()
        writer.writerows(rows)


def append_global_summary(
    path: Path,
    rows: list[dict],
) -> None:
    """Append result rows while preserving any columns added by future versions."""
    existing_rows: list[dict] = []
    fieldnames: list[str] = []
    if path.exists():
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames.extend(reader.fieldnames or [])
            existing_rows.extend(reader)

    for row in rows:
        for name in row:
            if name not in fieldnames:
                fieldnames.append(name)

    temp_path = path.with_suffix(path.suffix + ".tmp")
    write_csv(temp_path, existing_rows + rows, fieldnames=fieldnames)
    temp_path.replace(path)


GLOBAL_HISTORY_DIMENSIONS = {
    "run_id",
    "test_name",
    "completed_at_local",
    "result_directory",
    "provider",
    "product_name",
    "driver",
    "storage",
    "target_rate_per_s",
    "rate_label",
    "duration_seconds",
}


def metric_unit(metric: str) -> str:
    if metric.endswith("_ms"):
        return "ms"
    if metric.endswith("_pct"):
        return "%"
    if metric == "mean_excludes_first_trial":
        return "bool"
    if metric in {
        "attempts",
        "configured_max_workers",
        "effective_max_workers",
        "api_latency_mean_sample_count",
        "first_command_latency_mean_sample_count",
        "second_command_latency_mean_sample_count",
    }:
        return "count"
    return ""


def refresh_global_matrix(history_path: Path, matrix_path: Path) -> None:
    """Rebuild a latest-value vendor/rate matrix from the append-only history."""
    if not history_path.exists():
        return

    with history_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        history_rows = list(reader)
        history_fields = reader.fieldnames or []

    metric_fields = [
        field for field in history_fields if field not in GLOBAL_HISTORY_DIMENSIONS
    ]
    rates = sorted(
        {
            int(float(row["target_rate_per_s"]))
            for row in history_rows
            if row.get("target_rate_per_s", "").strip()
        }
    )
    rate_columns = [f"{rate}tps" for rate in rates]

    # History is append-only, so later rows intentionally replace earlier cells.
    matrix: dict[tuple[str, str, str, str, str, str, str], dict[str, str]] = {}
    for row in history_rows:
        raw_rate = row.get("target_rate_per_s", "").strip()
        if not raw_rate:
            continue
        rate_column = f"{int(float(raw_rate))}tps"
        for metric in metric_fields:
            value = row.get(metric, "")
            if value in (None, ""):
                continue
            key = (
                row.get("test_name", ""),
                row.get("provider", ""),
                row.get("product_name", row.get("provider", "")),
                row.get("driver", "").strip() or "e2b",
                row.get("storage", ""),
                row.get("duration_seconds", ""),
                metric,
            )
            matrix.setdefault(key, {})[rate_column] = value

    metric_order = {name: index for index, name in enumerate(metric_fields)}

    def duration_sort_value(value: str) -> tuple[int, float | str]:
        try:
            return (0, float(value))
        except ValueError:
            return (1, value)

    matrix_rows = []
    sorted_keys = sorted(
        matrix,
        key=lambda key: (
            key[0],
            key[4],
            duration_sort_value(key[5]),
            metric_order.get(key[6], len(metric_order)),
            key[1],
        ),
    )
    for (
        test_name,
        provider,
        product_name,
        driver,
        storage,
        duration_seconds,
        metric,
    ) in sorted_keys:
        values = matrix[
            (
                test_name,
                provider,
                product_name,
                driver,
                storage,
                duration_seconds,
                metric,
            )
        ]
        matrix_rows.append(
            {
                "test_name": test_name,
                "provider": provider,
                "product_name": product_name,
                "driver": driver,
                "storage": storage,
                "duration_seconds": duration_seconds,
                "metric": metric,
                "unit": metric_unit(metric),
                **{column: values.get(column, "") for column in rate_columns},
            }
        )

    fieldnames = [
        "test_name",
        "provider",
        "product_name",
        "driver",
        "storage",
        "duration_seconds",
        "metric",
        "unit",
        *rate_columns,
    ]
    temp_path = matrix_path.with_suffix(matrix_path.suffix + ".tmp")
    write_csv(temp_path, matrix_rows, fieldnames=fieldnames)
    temp_path.replace(matrix_path)


def write_failure_logs(
    output_dir: Path,
    title: str,
    results: list[TrialResult],
) -> None:
    failures = [
        row
        for row in results
        if not row.api_success or not row.ready_success or not row.cleanup_success
    ]
    fieldnames = list(asdict(results[0])) if results else []
    write_csv(
        output_dir / f"{title}_失败日志.csv",
        [asdict(row) for row in failures],
        fieldnames=fieldnames,
    )

    lines = [
        f"测试名称: {title}",
        f"总请求数: {len(results)}",
        f"失败请求数: {len(failures)}",
        "",
    ]
    if not failures:
        lines.append("本轮无失败。")
    else:
        for row in failures:
            lines.extend(
                [
                    "=" * 80,
                    f"trial_index: {row.trial_index}",
                    f"driver: {row.driver}",
                    f"scheduled_at_utc: {row.scheduled_at_utc}",
                    f"sandbox_id: {row.sandbox_id or '(未分配)'}",
                    f"api_success: {row.api_success}",
                    f"ready_success: {row.ready_success}",
                    f"cleanup_success: {row.cleanup_success}",
                    f"failure_phase: {row.failure_phase or '(无主流程错误)'}",
                    f"error_type: {row.error_type or '(无)'}",
                    f"error_message: {row.error_message or '(无)'}",
                    f"ready_poll_count: {row.ready_poll_count}",
                    (
                        "last_observed_status: "
                        f"{row.last_observed_status or '(无)'}"
                    ),
                ]
            )
            if row.error_traceback:
                lines.extend(["error_traceback:", row.error_traceback.rstrip()])
            if not row.cleanup_success:
                lines.extend(
                    [
                        f"cleanup_error_type: {row.cleanup_error_type or '(无)'}",
                        (
                            "cleanup_error_message: "
                            f"{row.cleanup_error_message or '(无)'}"
                        ),
                    ]
                )
                if row.cleanup_error_traceback:
                    lines.extend(
                        [
                            "cleanup_error_traceback:",
                            row.cleanup_error_traceback.rstrip(),
                        ]
                    )
            lines.append("")
    (output_dir / f"{title}_失败日志.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def result_title(results: list[TrialResult], test_name: str) -> str:
    storage_names = {
        "none": "无挂载",
        "oss": "对象存储OSS",
        "file": "文件存储NAS",
    }
    storages = list(dict.fromkeys(row.storage for row in results))
    rates = sorted({row.target_rate_per_s for row in results})
    durations = sorted({row.duration_seconds for row in results})
    product_names = {row.product_name for row in results}
    if len(product_names) != 1:
        raise RuntimeError("同一结果目录只能保存一个产品的测试结果")
    product_name = next(iter(product_names))
    product_part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", product_name).strip(
        " ."
    )
    if not product_part:
        product_part = results[0].provider
    storage_part = "-".join(storage_names.get(item, item) for item in storages)
    rate_part = "-".join(str(rate) for rate in rates) + "tps"
    duration_part = "-".join(
        str(int(value)) if float(value).is_integer() else f"{value:g}"
        for value in durations
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    raw_title = (
        f"{stamp}_{product_part}_{test_name}_{storage_part}_"
        f"{rate_part}_持续{duration_part}s"
    )
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_title).strip(" .")


def save_results(
    results: list[TrialResult],
    output_root: Path,
    test_name: str,
    add_to_global: bool = False,
    exclude_first_from_mean: bool = True,
) -> Path:
    if not results:
        raise RuntimeError("没有可保存的测试结果")
    providers = {row.provider for row in results}
    if len(providers) != 1:
        raise RuntimeError("同一结果目录只能保存一个厂商的测试结果")
    title = result_title(results, test_name)
    output_dir = output_root / title
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_rows = [asdict(row) for row in results]
    summary_rows = summarize(
        results,
        exclude_first_from_mean=exclude_first_from_mean,
    )
    write_csv(output_dir / f"{title}_原始明细.csv", raw_rows)
    write_csv(output_dir / f"{title}_汇总.csv", summary_rows)
    write_failure_logs(output_dir, title, results)
    (output_dir / f"{title}_汇总.json").write_text(
        json.dumps(summary_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if add_to_global:
        completed_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        global_rows = [
            {
                "run_id": title,
                "test_name": test_name,
                "completed_at_local": completed_at,
                "result_directory": title,
                **row,
            }
            for row in summary_rows
        ]
        history_path = output_root / "全局测试历史.csv"
        append_global_summary(history_path, global_rows)
        refresh_global_matrix(history_path, output_root / "全局测试结果.csv")
    return output_dir


def parse_csv_ints(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("并发档位必须是逗号分隔的正整数") from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("并发档位必须是逗号分隔的正整数")
    return values


def parse_storages(value: str) -> list[str]:
    values = [item.strip().lower() for item in value.split(",") if item.strip()]
    invalid = sorted(set(values) - set(STORAGE_CHOICES))
    if not values or invalid:
        raise argparse.ArgumentTypeError(
            f"存储类型只支持 {','.join(STORAGE_CHOICES)}；无效值: {','.join(invalid)}"
        )
    return list(dict.fromkeys(values))


def parse_provider(value: str) -> str:
    provider = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", provider):
        raise argparse.ArgumentTypeError(
            "厂商标识只允许小写字母、数字、下划线和短横线，最长 64 个字符"
        )
    return provider


def attempts_for(rates: list[int], duration: float, storages: list[str]) -> int:
    return sum(max(1, math.ceil(rate * duration)) for rate in rates) * len(storages)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="多产品云沙箱并发启动速度测试"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    smoke = subparsers.add_parser("smoke", help="只创建 1 个沙箱做连通性验证")
    smoke.add_argument(
        "--storage", choices=STORAGE_CHOICES, default="none", help="存储场景"
    )
    smoke.add_argument(
        "--provider", type=parse_provider, default=DEFAULT_PROVIDER, help="厂商标识"
    )
    smoke.add_argument("--output", type=Path, default=Path("results"))

    plan = subparsers.add_parser("plan", help="只计算测试规模，不调用云端")
    run = subparsers.add_parser("run", help="执行正式并发测试")
    run_config = subparsers.add_parser(
        "run-config",
        help="从 INI 配置文件选择产品和测试档位",
    )
    run_config.add_argument(
        "--config",
        type=Path,
        default=Path("benchmark.ini"),
        help="配置文件路径，默认 benchmark.ini",
    )
    run_config.add_argument(
        "--confirm",
        action="store_true",
        help="确认本次调用会创建可计费云资源",
    )
    for target in (plan, run):
        target.add_argument(
            "--provider",
            type=parse_provider,
            default=DEFAULT_PROVIDER,
            help=f"厂商标识，默认 {DEFAULT_PROVIDER}",
        )
        target.add_argument(
            "--rates",
            type=parse_csv_ints,
            default=[1, 10, 50, 100, 200],
            help="每秒发起创建数，默认 1,10,50,100,200",
        )
        target.add_argument(
            "--duration-seconds",
            type=float,
            default=1.0,
            help="每个档位发压持续秒数，默认 1",
        )
        target.add_argument(
            "--storages",
            type=parse_storages,
            default=["none"],
            help="none,oss,file 的任意组合，默认 none",
        )
    run.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"本地创建线程上限，默认 {DEFAULT_MAX_WORKERS}",
    )
    run.add_argument("--output", type=Path, default=Path("results"))
    run.add_argument(
        "--confirm",
        action="store_true",
        help="确认本次调用会创建可计费云资源",
    )
    return parser


def print_plan(
    provider: str,
    product_name: str,
    rates: list[int],
    duration: float,
    storages: list[str],
    rate_durations: list[float] | None = None,
    rate_interval_seconds: float = 0.0,
) -> None:
    """Print the test plan.

    rate_durations: per-rate durations overriding the global duration.
    rate_interval_seconds: uniform pause between rates.
    """
    print(f"产品: {product_name} ({provider})")
    print(f"存储场景: {', '.join(storages)}")
    print(f"并发档位: {', '.join(str(rate) + '/s' for rate in rates)}")
    has_per_rate = rate_durations is not None and rate_durations != [duration] * len(rates)
    if not has_per_rate:
        print(f"每档持续: {duration:g} 秒")
    if rate_interval_seconds > 0:
        print(f"档位间隔: {rate_interval_seconds:g} 秒")
    total_attempts = 0
    for storage in storages:
        for i, rate in enumerate(rates):
            dur = rate_durations[i] if rate_durations else duration
            n = max(1, math.ceil(rate * dur))
            total_attempts += n
            if has_per_rate:
                print(
                    f"  {storage:>4} @ {rate:>3}/s: "
                    f"{n} 次创建  持续 {dur:g}s"
                )
            else:
                print(
                    f"  {storage:>4} @ {rate:>3}/s: "
                    f"{n} 次创建"
                )
    print(f"预计 Sandbox.create 总次数: {total_attempts}")


def execute_run(
    *,
    settings: Settings,
    provider: str,
    product_name: str,
    rates: list[int],
    duration_seconds: float,
    storages: list[str],
    max_workers: int,
    output: Path,
    confirm: bool,
    exclude_first_from_mean: bool,
    rate_durations: list[float] | None = None,
    rate_interval_seconds: float = 0.0,
) -> int:
    """Execute the benchmark run.

    rate_durations: per-rate durations (same length as rates). Falls back to
                    duration_seconds for each rate when not provided.
    rate_interval_seconds: seconds to sleep after each rate completes before
                    starting the next one. Applied uniformly across all rates.
    """
    if duration_seconds <= 0:
        raise RuntimeError("duration_seconds 必须大于 0")
    if max_workers <= 0:
        raise RuntimeError("max_workers 必须大于 0")

    # Build effective per-rate durations.
    effective_durations = rate_durations if rate_durations else [duration_seconds] * len(rates)

    for storage in storages:
        metadata_for(storage, settings)
    print_plan(
        provider,
        product_name,
        rates,
        duration_seconds,
        storages,
        rate_durations=rate_durations,
        rate_interval_seconds=rate_interval_seconds,
    )
    print(f"SDK 驱动: {settings.driver}")
    if isinstance(settings, VolcengineNativeSettings):
        print(
            "火山原生就绪口径: first=首次 DescribeSandbox Ready，"
            "second=第二次 DescribeSandbox Ready 确认；均从 Create 开始计时"
        )
    print(
        "均值口径: "
        + (
            "每个档位第1个沙箱不计入 mean，但仍计入 min/max/百分位"
            if exclude_first_from_mean
            else "所有有效样本均计入 mean"
        )
    )
    if not confirm:
        print("\n未执行：正式测试请在命令末尾加 --confirm。")
        return 2

    native_runtime = (
        VolcengineNativeRuntime(settings, max_workers)
        if isinstance(settings, VolcengineNativeSettings)
        else None
    )
    all_results: list[TrialResult] = []
    try:
        for storage in storages:
            for i, rate in enumerate(rates):
                rate_dur = effective_durations[i]
                print(
                    f"\n开始: product={product_name}, storage={storage}, "
                    f"rate={rate}/s, duration={rate_dur:g}s"
                )
                all_results.extend(
                    run_rate(
                        settings=settings,
                        provider=provider,
                        product_name=product_name,
                        storage=storage,
                        rate=rate,
                        duration_seconds=rate_dur,
                        max_workers=max_workers,
                        native_runtime=native_runtime,
                    )
                )
                is_last = (i == len(rates) - 1)
                if rate_interval_seconds > 0 and not is_last:
                    print(
                        f"  档位 {rate}/s 完成，等待 {rate_interval_seconds:g}s 后进入下一档...",
                        flush=True,
                    )
                    time.sleep(rate_interval_seconds)
    finally:
        if native_runtime is not None:
            native_runtime.close()
    output_dir = save_results(
        all_results,
        output,
        "启动并发速度",
        add_to_global=True,
        exclude_first_from_mean=exclude_first_from_mean,
    )
    print(f"\n完成。结果目录: {output_dir.resolve()}")
    print(
        json.dumps(
            summarize(
                all_results,
                exclude_first_from_mean=exclude_first_from_mean,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "plan":
        if args.duration_seconds <= 0:
            raise RuntimeError("--duration-seconds 必须大于 0")
        print_plan(
            args.provider,
            DEFAULT_PRODUCT_NAMES.get(args.provider, args.provider),
            args.rates,
            args.duration_seconds,
            args.storages,
        )
        return 0

    if args.command == "run-config":
        configured = load_configured_run(args.config)
        return execute_run(
            settings=configured.settings,
            provider=configured.provider,
            product_name=configured.product_name,
            rates=configured.rates,
            duration_seconds=configured.duration_seconds,
            storages=configured.storages,
            max_workers=configured.max_workers,
            output=configured.output,
            confirm=args.confirm,
            exclude_first_from_mean=configured.exclude_first_from_mean,
            rate_durations=configured.rate_durations,
            rate_interval_seconds=configured.rate_interval_seconds,
        )

    settings = load_settings(require_credentials=True)
    if args.command == "smoke":
        metadata_for(args.storage, settings)
        print(f"开始 smoke 测试，存储场景={args.storage}")
        result, sandbox = one_trial(
            settings=settings,
            provider=args.provider,
            product_name=DEFAULT_PRODUCT_NAMES.get(args.provider, args.provider),
            storage=args.storage,
            rate=1,
            duration_seconds=1.0,
            configured_max_workers=1,
            effective_max_workers=1,
            trial_index=1,
            scheduled_monotonic=time.perf_counter(),
            scheduled_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        # smoke 测试为单次创建，结束后立即清理
        if sandbox is not None:
            try:
                ok = bool(sandbox.kill())
                if not ok:
                    result.cleanup_success = False
                    result.cleanup_error_type = "CleanupReturnedFalse"
                    result.cleanup_error_message = "sandbox.kill() 返回 False"
            except Exception as exc:
                result.cleanup_success = False
                result.cleanup_error_type = type(exc).__name__
                result.cleanup_error_message = (
                    str(exc).replace("\r", " ").replace("\n", " ")[:1000]
                )
                result.cleanup_error_traceback = traceback.format_exc()
        output_dir = save_results([result], args.output, "连通性测试")
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        print(f"结果目录: {output_dir.resolve()}")
        return 0 if result.ready_success and result.cleanup_success else 1

    return execute_run(
        settings=settings,
        provider=args.provider,
        product_name=DEFAULT_PRODUCT_NAMES.get(args.provider, args.provider),
        rates=args.rates,
        duration_seconds=args.duration_seconds,
        storages=args.storages,
        max_workers=args.max_workers,
        output=args.output,
        confirm=args.confirm,
        exclude_first_from_mean=True,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户中断。已提交任务会在 finally 中尝试释放沙箱。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        if os.environ.get("DEBUG", "").lower() in {"1", "true", "yes"}:
            traceback.print_exc()
        raise SystemExit(1)
