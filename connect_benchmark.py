from __future__ import annotations

import argparse
import json
import math
import statistics
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from e2b_code_interpreter import Sandbox

import benchmark as base


TEST_NAME = "连接并发速度"
T = TypeVar("T")
PRINT_LOCK = threading.Lock()


@dataclass
class ConnectTrialResult:
    provider: str
    product_name: str
    target_template: str
    driver: str
    storage: str
    target_rate_per_s: int
    sandbox_count: int
    configured_max_workers: int
    effective_max_workers: int
    trial_index: int
    create_latency_ms: float | None = None
    pause_scheduled_at_utc: str = ""
    pause_queue_delay_ms: float | None = None
    pause_latency_ms: float | None = None
    connect_scheduled_at_utc: str = ""
    connect_queue_delay_ms: float | None = None
    connect_latency_ms: float | None = None
    first_access_latency_ms: float | None = None
    second_access_latency_ms: float | None = None
    create_success: bool = False
    initial_access_success: bool = False
    pause_success: bool = False
    connect_success: bool = False
    access_success: bool = False
    cleanup_success: bool = False
    sandbox_id: str = ""
    failure_phase: str = ""
    error_type: str = ""
    error_message: str = ""
    error_traceback: str = ""
    cleanup_error_type: str = ""
    cleanup_error_message: str = ""
    cleanup_error_traceback: str = ""
    initial_rest_poll_count: int = 0
    initial_rest_last_status: str = ""
    first_rest_poll_count: int = 0
    second_rest_poll_count: int = 0
    first_rest_last_status: str = ""
    second_rest_last_status: str = ""


def record_failure(result: ConnectTrialResult, phase: str, exc: BaseException) -> None:
    if result.failure_phase:
        return
    result.failure_phase = phase
    result.error_type = type(exc).__name__
    result.error_message = str(exc).replace("\r", " ").replace("\n", " ")[:1000]
    result.error_traceback = traceback.format_exc()


def run_scheduled(
    items: list[T],
    *,
    rate: int,
    max_workers: int,
    action: Callable[[T, float, str], None],
    progress_label: str,
) -> None:
    if not items:
        return
    effective_workers = min(max_workers, len(items))
    start = time.perf_counter() + 0.25
    utc_start = datetime.now(timezone.utc).timestamp() + 0.25
    futures: list[Future[None]] = []

    with ThreadPoolExecutor(
        max_workers=effective_workers,
        thread_name_prefix=f"sandbox-{progress_label}",
    ) as pool:
        for index, item in enumerate(items):
            scheduled = start + index / rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            scheduled_utc = datetime.fromtimestamp(
                utc_start + index / rate,
                tz=timezone.utc,
            ).isoformat()
            futures.append(pool.submit(action, item, scheduled, scheduled_utc))

        completed = 0
        for future in as_completed(futures):
            future.result()
            completed += 1
            if completed == len(items) or completed % max(1, len(items) // 10) == 0:
                with PRINT_LOCK:
                    print(
                        f"  {progress_label}进度 {completed}/{len(items)}",
                        flush=True,
                    )


def run_rest_access(
    result: ConnectTrialResult,
    sandbox: Sandbox,
    settings: base.E2BSettings,
    connect_start: float,
) -> None:
    host = sandbox.get_host(settings.rest_port)
    path = settings.rest_path
    if not path.startswith("/"):
        path = "/" + path
    url = f"https://{host}{path}"
    access_token = getattr(sandbox, "traffic_access_token", None) or ""
    headers = {}
    if access_token:
        headers["e2b-traffic-access-token"] = access_token

    first_deadline = connect_start + base.REST_CHECK_TIMEOUT_S
    while True:
        try:
            response = base.rest_session().get(url, headers=headers, timeout=10)
            result.first_rest_poll_count += 1
            result.first_rest_last_status = str(response.status_code)
            if response.status_code == 200:
                break
        except Exception:
            result.first_rest_poll_count += 1

        remaining = first_deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(
                "首次 REST 检查超时；"
                f"polls={result.first_rest_poll_count}; "
                f"last_status={result.first_rest_last_status or '(无响应)'}; "
                f"url={url}"
            )
        time.sleep(min(base.REST_CHECK_POLL_INTERVAL_S, remaining))

    result.first_access_latency_ms = (
        time.perf_counter() - connect_start
    ) * 1000
    if result.first_rest_poll_count > 1:
        print(
            "  REST connect first health check "
            f"trial={result.trial_index} "
            f"retries={result.first_rest_poll_count - 1} "
            f"polls={result.first_rest_poll_count} final_status=200",
            flush=True,
        )

    second_deadline = time.perf_counter() + base.REST_CHECK_TIMEOUT_S
    while True:
        try:
            response = base.rest_session().get(url, headers=headers, timeout=10)
            result.second_rest_poll_count += 1
            result.second_rest_last_status = str(response.status_code)
            if response.status_code == 200:
                break
        except Exception:
            result.second_rest_poll_count += 1

        remaining = second_deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(
                "第二次 REST 检查超时；"
                f"polls={result.second_rest_poll_count}; "
                f"last_status={result.second_rest_last_status or '(无响应)'}; "
                f"url={url}"
            )
        time.sleep(min(base.REST_CHECK_POLL_INTERVAL_S, remaining))

    result.second_access_latency_ms = (
        time.perf_counter() - connect_start
    ) * 1000
    if result.second_rest_poll_count > 1:
        print(
            "  REST connect second health check "
            f"trial={result.trial_index} "
            f"retries={result.second_rest_poll_count - 1} "
            f"polls={result.second_rest_poll_count} final_status=200",
            flush=True,
        )


def run_command_access(
    result: ConnectTrialResult,
    sandbox: Sandbox,
    connect_start: float,
) -> None:
    first = sandbox.commands.run(
        "python3 -c \"print('SANDBOX_CONNECT_FIRST_COMMAND')\"",
        timeout=30,
    )
    result.first_access_latency_ms = (
        time.perf_counter() - connect_start
    ) * 1000
    if "SANDBOX_CONNECT_FIRST_COMMAND" not in (getattr(first, "stdout", "") or ""):
        raise RuntimeError("connect 后首条命令未返回预期标记")

    second = sandbox.commands.run(
        "python3 -c \"print('SANDBOX_CONNECT_SECOND_COMMAND')\"",
        timeout=30,
    )
    result.second_access_latency_ms = (
        time.perf_counter() - connect_start
    ) * 1000
    if "SANDBOX_CONNECT_SECOND_COMMAND" not in (
        getattr(second, "stdout", "") or ""
    ):
        raise RuntimeError("connect 后第二条命令未返回预期标记")


def verify_created_sandbox(
    result: ConnectTrialResult,
    sandbox: Sandbox,
    settings: base.E2BSettings,
) -> None:
    """Verify one successful access before the sandbox is paused."""
    if settings.check_type == base.CHECK_TYPE_COMMAND:
        command_result = sandbox.commands.run(
            "python3 -c \"print('SANDBOX_PRE_PAUSE_CHECK')\"",
            timeout=30,
        )
        stdout = getattr(command_result, "stdout", "") or ""
        if "SANDBOX_PRE_PAUSE_CHECK" not in stdout:
            raise RuntimeError("暂停前访问命令未返回预期标记")
        return

    host = sandbox.get_host(settings.rest_port)
    path = settings.rest_path
    if not path.startswith("/"):
        path = "/" + path
    url = f"https://{host}{path}"
    access_token = getattr(sandbox, "traffic_access_token", None) or ""
    headers = {}
    if access_token:
        headers["e2b-traffic-access-token"] = access_token

    deadline = time.perf_counter() + base.REST_CHECK_TIMEOUT_S
    while True:
        try:
            response = base.rest_session().get(url, headers=headers, timeout=10)
            result.initial_rest_poll_count += 1
            result.initial_rest_last_status = str(response.status_code)
            if response.status_code == 200:
                break
        except Exception:
            result.initial_rest_poll_count += 1

        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(
                "暂停前 REST 检查超时；"
                f"polls={result.initial_rest_poll_count}; "
                f"last_status={result.initial_rest_last_status or '(无响应)'}; "
                f"url={url}"
            )
        time.sleep(min(base.REST_CHECK_POLL_INTERVAL_S, remaining))

    if result.initial_rest_poll_count > 1:
        print(
            "  REST pre-pause health check "
            f"trial={result.trial_index} "
            f"retries={result.initial_rest_poll_count - 1} "
            f"polls={result.initial_rest_poll_count} final_status=200",
            flush=True,
        )


def run_one_rate(
    *,
    settings: base.E2BSettings,
    provider: str,
    product_name: str,
    storage: str,
    rate: int,
    sandbox_count: int,
    max_workers: int,
) -> list[ConnectTrialResult]:
    effective_workers = min(max_workers, sandbox_count)
    results = [
        ConnectTrialResult(
            provider=provider,
            product_name=product_name,
            target_template=settings.template,
            driver=settings.driver,
            storage=storage,
            target_rate_per_s=rate,
            sandbox_count=sandbox_count,
            configured_max_workers=max_workers,
            effective_max_workers=effective_workers,
            trial_index=index + 1,
        )
        for index in range(sandbox_count)
    ]
    sandboxes: dict[int, Sandbox] = {}
    sandbox_lock = threading.Lock()

    def create_one(
        result: ConnectTrialResult,
        _scheduled: float,
        _scheduled_utc: str,
    ) -> None:
        started = time.perf_counter()
        try:
            sandbox = Sandbox.create(
                template=settings.template,
                timeout=settings.sandbox_timeout,
                api_key=settings.api_key,
                api_url=settings.api_url,
                domain=settings.domain,
                metadata=base.metadata_for(storage, settings),
            )
            result.create_latency_ms = (time.perf_counter() - started) * 1000
            result.sandbox_id = str(
                getattr(sandbox, "sandbox_id", getattr(sandbox, "id", "")) or ""
            )
            if not result.sandbox_id:
                raise RuntimeError("Sandbox.create 成功但没有返回 sandbox_id")
            result.create_success = True
            with sandbox_lock:
                sandboxes[result.trial_index] = sandbox
            verify_created_sandbox(result, sandbox, settings)
            result.initial_access_success = True
        except Exception as exc:
            phase = "initial_access" if result.create_success else "create"
            record_failure(result, phase, exc)

    def pause_one(
        result: ConnectTrialResult,
        scheduled: float,
        scheduled_utc: str,
    ) -> None:
        result.pause_scheduled_at_utc = scheduled_utc
        actual_start = time.perf_counter()
        result.pause_queue_delay_ms = max(0.0, (actual_start - scheduled) * 1000)
        try:
            sandbox = sandboxes[result.trial_index]
            paused = sandbox.pause()
            result.pause_latency_ms = (time.perf_counter() - actual_start) * 1000
            if not paused:
                raise RuntimeError("sandbox.pause() 返回 False")
            result.pause_success = True
        except Exception as exc:
            record_failure(result, "pause", exc)

    def connect_one(
        result: ConnectTrialResult,
        scheduled: float,
        scheduled_utc: str,
    ) -> None:
        result.connect_scheduled_at_utc = scheduled_utc
        actual_start = time.perf_counter()
        result.connect_queue_delay_ms = max(0.0, (actual_start - scheduled) * 1000)
        connect_start = time.perf_counter()
        try:
            sandbox = sandboxes[result.trial_index]
            connected = sandbox.connect(timeout=settings.sandbox_timeout)
            result.connect_latency_ms = (time.perf_counter() - connect_start) * 1000
            result.connect_success = True
            with sandbox_lock:
                sandboxes[result.trial_index] = connected

            if settings.check_type == base.CHECK_TYPE_REST:
                run_rest_access(result, connected, settings, connect_start)
            else:
                run_command_access(result, connected, connect_start)
            result.access_success = True
        except Exception as exc:
            phase = "access" if result.connect_success else "connect"
            record_failure(result, phase, exc)

    def cleanup_one(result: ConnectTrialResult) -> None:
        sandbox = sandboxes.get(result.trial_index)
        if sandbox is None:
            result.cleanup_success = True
            return
        try:
            result.cleanup_success = bool(sandbox.kill())
            if not result.cleanup_success:
                result.cleanup_error_type = "CleanupReturnedFalse"
                result.cleanup_error_message = "sandbox.kill() 返回 False"
        except Exception as exc:
            result.cleanup_success = False
            result.cleanup_error_type = type(exc).__name__
            result.cleanup_error_message = (
                str(exc).replace("\r", " ").replace("\n", " ")[:1000]
            )
            result.cleanup_error_traceback = traceback.format_exc()

    try:
        print("  阶段 1/4：创建沙箱并验证访问", flush=True)
        run_scheduled(
            results,
            rate=rate,
            max_workers=max_workers,
            action=create_one,
            progress_label="创建",
        )
        base.close_rest_sessions()

        pausable = [
            result
            for result in results
            if result.create_success and result.initial_access_success
        ]
        print("  阶段 2/4：暂停沙箱", flush=True)
        run_scheduled(
            pausable,
            rate=rate,
            max_workers=max_workers,
            action=pause_one,
            progress_label="暂停",
        )

        connectable = [result for result in pausable if result.pause_success]
        print("  阶段 3/4：connect 并执行两次访问", flush=True)
        run_scheduled(
            connectable,
            rate=rate,
            max_workers=max_workers,
            action=connect_one,
            progress_label="connect",
        )
    finally:
        base.close_rest_sessions()
        print("  阶段 4/4：统一删除本档沙箱", flush=True)
        with ThreadPoolExecutor(
            max_workers=effective_workers,
            thread_name_prefix="sandbox-cleanup",
        ) as pool:
            futures = [pool.submit(cleanup_one, result) for result in results]
            for future in as_completed(futures):
                future.result()

    return results


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return round(ordered[index], 3)


def add_metric_stats(
    row: dict,
    prefix: str,
    all_values: list[float],
    mean_values: list[float],
) -> None:
    row[f"{prefix}_min_ms"] = round(min(all_values), 3) if all_values else None
    row[f"{prefix}_max_ms"] = round(max(all_values), 3) if all_values else None
    row[f"{prefix}_mean_sample_count"] = len(mean_values)
    row[f"{prefix}_mean_ms"] = (
        round(statistics.fmean(mean_values), 3) if mean_values else None
    )
    for percent in (50, 90, 95, 99):
        row[f"{prefix}_p{percent}_ms"] = percentile(all_values, percent)


def summarize(
    results: list[ConnectTrialResult],
    exclude_first_from_mean: bool,
) -> list[dict]:
    groups: dict[tuple, list[ConnectTrialResult]] = {}
    for result in results:
        key = (
            result.provider,
            result.product_name,
            result.target_template,
            result.driver,
            result.storage,
            result.target_rate_per_s,
            result.sandbox_count,
            result.configured_max_workers,
            result.effective_max_workers,
        )
        groups.setdefault(key, []).append(result)

    rows: list[dict] = []
    for key, group in sorted(groups.items()):
        (
            provider,
            product_name,
            target_template,
            driver,
            storage,
            rate,
            sandbox_count,
            configured_workers,
            effective_workers,
        ) = key
        attempts = len(group)
        mean_group = [
            item
            for item in group
            if not exclude_first_from_mean or item.trial_index != 1
        ]
        row = {
            "provider": provider,
            "product_name": product_name,
            "target_template": target_template,
            "driver": driver,
            "storage": storage,
            "target_rate_per_s": rate,
            "rate_label": f"{rate}tps",
            "sandbox_count": sandbox_count,
            "configured_max_workers": configured_workers,
            "effective_max_workers": effective_workers,
            "attempts": attempts,
            "mean_excludes_first_trial": exclude_first_from_mean,
            "create_success_rate_pct": round(
                100 * sum(item.create_success for item in group) / attempts, 3
            ),
            "initial_access_success_rate_pct": round(
                100 * sum(item.initial_access_success for item in group) / attempts,
                3,
            ),
            "pause_success_rate_pct": round(
                100 * sum(item.pause_success for item in group) / attempts, 3
            ),
            "connect_success_rate_pct": round(
                100 * sum(item.connect_success for item in group) / attempts, 3
            ),
            "access_success_rate_pct": round(
                100 * sum(item.access_success for item in group) / attempts, 3
            ),
            "cleanup_success_rate_pct": round(
                100 * sum(item.cleanup_success for item in group) / attempts, 3
            ),
        }
        for field in (
            "create_latency_ms",
            "pause_latency_ms",
            "connect_latency_ms",
            "first_access_latency_ms",
            "second_access_latency_ms",
        ):
            prefix = field.removesuffix("_ms")
            all_values = [
                value
                for item in group
                if (value := getattr(item, field)) is not None
            ]
            mean_values = [
                value
                for item in mean_group
                if (value := getattr(item, field)) is not None
            ]
            add_metric_stats(row, prefix, all_values, mean_values)

        pause_queue = [
            item.pause_queue_delay_ms
            for item in group
            if item.pause_queue_delay_ms is not None
        ]
        connect_queue = [
            item.connect_queue_delay_ms
            for item in group
            if item.connect_queue_delay_ms is not None
        ]
        row["pause_schedule_delay_p95_ms"] = percentile(pause_queue, 95)
        row["connect_schedule_delay_p95_ms"] = percentile(connect_queue, 95)
        rows.append(row)
    return rows


def write_failure_logs(
    output_dir: Path,
    title: str,
    results: list[ConnectTrialResult],
) -> None:
    failures = [
        result
        for result in results
        if not (
            result.create_success
            and result.initial_access_success
            and result.pause_success
            and result.connect_success
            and result.access_success
            and result.cleanup_success
        )
    ]
    fields = list(asdict(results[0])) if results else []
    base.write_csv(
        output_dir / f"{title}_失败日志.csv",
        [asdict(result) for result in failures],
        fieldnames=fields,
    )
    lines = [
        f"测试名称: {title}",
        f"总请求数: {len(results)}",
        f"失败请求数: {len(failures)}",
        "",
    ]
    if not failures:
        lines.append("本轮无失败。")
    for result in failures:
        lines.extend(
            [
                "=" * 80,
                f"trial_index: {result.trial_index}",
                f"sandbox_id: {result.sandbox_id or '(未分配)'}",
                f"initial_access_success: {result.initial_access_success}",
                f"failure_phase: {result.failure_phase or '(仅清理失败)'}",
                f"error_type: {result.error_type or '(无)'}",
                f"error_message: {result.error_message or '(无)'}",
                f"cleanup_error_type: {result.cleanup_error_type or '(无)'}",
                f"cleanup_error_message: {result.cleanup_error_message or '(无)'}",
                "",
            ]
        )
    (output_dir / f"{title}_失败日志.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def save_results(
    results: list[ConnectTrialResult],
    output_root: Path,
    exclude_first_from_mean: bool,
) -> Path:
    if not results:
        raise RuntimeError("没有可保存的 connect 测试结果")
    title = base.result_title(results, TEST_NAME)
    output_dir = output_root / title
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_rows = [asdict(result) for result in results]
    summary_rows = summarize(results, exclude_first_from_mean)
    base.write_csv(output_dir / f"{title}_原始明细.csv", raw_rows)
    base.write_csv(output_dir / f"{title}_汇总.csv", summary_rows)
    write_failure_logs(output_dir, title, results)
    (output_dir / f"{title}_汇总.json").write_text(
        json.dumps(summary_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    completed_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    global_rows = [
        {
            "run_id": title,
            "test_name": TEST_NAME,
            "completed_at_local": completed_at,
            "result_directory": title,
            **row,
        }
        for row in summary_rows
    ]
    history_path = output_root / "全局测试历史.csv"
    base.append_global_summary(history_path, global_rows)
    base.refresh_global_matrix(history_path, output_root / "全局测试结果.csv")
    return output_dir


def print_plan(config: base.ConfiguredRun) -> None:
    print(f"测试类型: {config.test_type}")
    print(f"产品: {config.product_name} ({config.provider})")
    print(f"存储场景: {', '.join(config.storages)}")
    print(f"模板: {config.settings.template}")
    print(f"Domain: {config.settings.domain}")
    print(f"检查方式: {config.settings.check_type}")
    total = 0
    for storage in config.storages:
        for index, rate in enumerate(config.rates):
            count = config.sandbox_counts[index]
            total += count
            print(
                f"  {storage:>4} @ {rate:>3}/s: 创建并验证访问 {count}，"
                f"暂停 {count}，connect {count}，最后统一删除"
            )
    print(f"预计 Sandbox.create 总次数: {total}")
    print(f"预计 pause 总次数: {total}")
    print(f"预计 connect 总次数: {total}")


def execute(config: base.ConfiguredRun, confirm: bool) -> int:
    if config.test_type != "connect":
        raise RuntimeError(
            "connect_benchmark.py 要求 [run] test_type = connect"
        )
    if not isinstance(config.settings, base.E2BSettings):
        raise RuntimeError("connect benchmark 当前只支持 E2B SDK 驱动")
    if config.max_workers <= 0:
        raise RuntimeError("max_workers 必须大于 0")

    for storage in config.storages:
        base.metadata_for(storage, config.settings)
    print_plan(config)
    print(
        "均值口径: "
        + (
            "每档第1个沙箱不计入 mean，但仍计入 min/max/百分位"
            if config.exclude_first_from_mean
            else "所有有效样本均计入 mean"
        )
    )
    if not confirm:
        print("\n未执行：正式测试请在命令末尾加 --confirm。")
        return 2

    all_results: list[ConnectTrialResult] = []
    for storage in config.storages:
        for index, rate in enumerate(config.rates):
            count = config.sandbox_counts[index]
            print(
                f"\n开始: product={config.product_name}, storage={storage}, "
                f"rate={rate}/s, sandbox_count={count}, "
                f"template={config.settings.template}, domain={config.settings.domain}",
                flush=True,
            )
            rate_results = run_one_rate(
                settings=config.settings,
                provider=config.provider,
                product_name=config.product_name,
                storage=storage,
                rate=rate,
                sandbox_count=count,
                max_workers=config.max_workers,
            )
            all_results.extend(rate_results)
            print(f"  档位 {rate}/s 已完成并删除沙箱", flush=True)

            is_last = index == len(config.rates) - 1
            if config.rate_interval_seconds > 0 and not is_last:
                print(
                    f"  等待 {config.rate_interval_seconds:g}s 后进入下一档...",
                    flush=True,
                )
                time.sleep(config.rate_interval_seconds)

    output_dir = save_results(
        all_results,
        config.output,
        config.exclude_first_from_mean,
    )
    print(f"\n完成。结果目录: {output_dir.resolve()}")
    print(
        json.dumps(
            summarize(all_results, config.exclude_first_from_mean),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E2B pause/connect 并发性能测试")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan", "读取配置并显示测试计划，不调用云端"),
        ("run-config", "读取配置并执行 connect 测试"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument(
            "--config",
            type=Path,
            default=Path("benchmark.ini"),
            help="配置文件路径，默认 benchmark.ini",
        )
        if name == "run-config":
            command.add_argument(
                "--confirm",
                action="store_true",
                help="确认本次调用会创建可计费云资源",
            )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = base.load_configured_run(args.config)
    if args.command == "plan":
        if config.test_type != "connect":
            raise RuntimeError("请在 [run] 中设置 test_type = connect")
        if not isinstance(config.settings, base.E2BSettings):
            raise RuntimeError("connect benchmark 当前只支持 E2B SDK 驱动")
        print_plan(config)
        return 0
    return execute(config, args.confirm)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户中断。", flush=True)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误: {exc}", flush=True)
        raise
