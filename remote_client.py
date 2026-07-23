"""
通用远程脚本执行 - 客户端

轻量 HTTP 客户端，不依赖任何业务逻辑，只做：
  1. 上传文件
  2. 远程执行脚本（脚本名 + 参数）
  3. 轮询等待完成
  4. 下载结果文件

用法:
    from remote_client import RemoteScriptClient

    client = RemoteScriptClient("http://your-server:8000")
    task_id = client.upload("input.mp3")
    client.run("audio_translate.py", ["input.mp3", "-t", "zh"], task_id=task_id)
    client.wait(task_id)
    client.download(task_id, "output/final.wav", "final.wav")
"""

import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests


def normalize_server_url(server: str, port: int = 8000) -> str:
    """将用户输入的服务端地址规范化为完整 URL。

    支持的输入形式：
      - "1.2.3.4"              → "http://1.2.3.4:8000"
      - "my-server"            → "http://my-server:8000"
      - "http://1.2.3.4/"      → "http://1.2.3.4:8000"
      - "http://1.2.3.4:9000/" → "http://1.2.3.4:9000"
      - "https://example.com"  → "https://example.com:8000"
    """
    if server.startswith("http://") or server.startswith("https://"):
        parsed = urlparse(server)
        host = parsed.hostname
        if not host:
            # URL 解析失败，回退为原样返回
            return server
        parsed_port = parsed.port
        final_port = parsed_port if parsed_port else port
        return f"{parsed.scheme}://{host}:{final_port}"
    return f"http://{server}:{port}"


def resolve_via_scheduler(scheduler: str, port: int = 8000, timeout: float = 15.0) -> str:
    """向调度器请求分配一个空闲服务端节点，返回该节点 URL。

    参数:
        scheduler: 调度器地址，支持 IP、域名或完整 URL（同 normalize_server_url）
        port: 调度器默认端口（当地址未显式带端口时使用）
        timeout: 请求超时（秒）

    返回:
        分配到的服务端节点 URL，如 "http://1.2.3.4:8000"

    异常:
        ConnectionError: 调度器不可达
        RuntimeError: 调度器无空闲节点（全忙）或其他分配错误
    """
    scheduler_url = normalize_server_url(scheduler, port=port)
    try:
        resp = requests.get(f"{scheduler_url}/allocate", timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(
            f"无法连接调度器: {scheduler_url}\n"
            f"  请确认调度器已启动且端口正确。\n"
            f"  原始错误: {e}"
        )
    except requests.exceptions.Timeout:
        raise ConnectionError(f"连接调度器超时 ({timeout}s): {scheduler_url}")

    if resp.status_code == 200:
        worker_url = resp.json()["server_url"]
        print(f"已通过调度器 {scheduler_url} 分配空闲服务端: {worker_url}")
        return worker_url

    # 503 = 全部忙碌 / 暂无注册节点；从 detail 中提取可读信息
    message = None
    try:
        detail = resp.json().get("detail")
        if isinstance(detail, dict):
            message = detail.get("message")
        elif isinstance(detail, str):
            message = detail
    except ValueError:
        pass
    if not message:
        message = f"调度器分配失败 (HTTP {resp.status_code}): {scheduler_url}"
    raise RuntimeError(message)


def resolve_server_arg(server: str, scheduler: Optional[str] = None) -> str:
    """根据 --server / --scheduler 命令行参数解析最终服务端 URL。

    - scheduler 非空：向调度器请求分配空闲节点（新模式）
    - 否则：直连 --server 指定的地址（老模式，行为不变）

    异常:
        ConnectionError: 调度器不可达
        RuntimeError: 调度器无空闲节点
    """
    if scheduler:
        return resolve_via_scheduler(scheduler)
    return normalize_server_url(server)


class RemoteScriptClient:
    """通用远程脚本执行客户端"""

    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 timeout: tuple[float, float] | float = (30, 300)):
        """
        参数:
            base_url: 服务端地址，如 "http://<ServerIP>:8000"
            api_key: 可选，API 密钥（预留，服务端暂未实现鉴权）
            timeout: 单次 HTTP 请求超时（秒），支持两种格式：
                     - 元组 (connect_timeout, read_timeout)，如 (30, 300)
                       connect_timeout: 建立连接的超时（秒），默认 30s
                                        （跨公网瞬时延迟较常见，太小容易误判断开）
                       read_timeout: 等待响应的超时（秒），默认 300s
                     - 单个数字: 同时用于连接和读取（兼容旧用法）
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        """构建请求头"""
        h = {}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ─── 健康检查 ──────────────────────────────────────────

    def check_server(self, timeout: float = 10.0) -> None:
        """验证服务端可达性，不可达时抛出 ConnectionError。

        在执行任何业务操作前调用，避免后续请求卡死。
        使用独立的短超时，不受 self.timeout 影响。

        异常:
            ConnectionError: 服务端不可达或响应异常
        """
        try:
            resp = requests.get(
                f"{self.base_url}/health",
                headers=self._headers(),
                timeout=timeout,
            )
            if resp.status_code != 200:
                raise ConnectionError(
                    f"服务端响应异常 (HTTP {resp.status_code}): {self.base_url}"
                )
        except requests.exceptions.ConnectionError as e:
            raise ConnectionError(
                f"无法连接服务端: {self.base_url}\n"
                f"  请确认服务端已启动且端口正确。\n"
                f"  原始错误: {e}"
            )
        except requests.exceptions.Timeout:
            raise ConnectionError(
                f"连接服务端超时 ({timeout}s): {self.base_url}\n"
                f"  可能原因：端口被占用（如 Docker Desktop 端口残留），服务端进程僵死。"
            )

    # ─── 任务目录检查 ────────────────────────────────────────

    def task_exists(self, task_id: str) -> bool:
        """检查 task_id 对应的任务目录是否存在于服务端。

        用于客户端断点续跑前验证旧 task_id 是否仍然有效
        （服务端 workspace 可能被清理或重部署）。
        """
        try:
            resp = requests.get(
                f"{self.base_url}/files/{task_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    # ─── 上传文件 ──────────────────────────────────────────

    def upload(self, file_path: str | Path,
               task_id: Optional[str] = None,
               dest_path: Optional[str] = None) -> dict:
        """
        上传文件到服务端

        参数:
            file_path: 本地文件路径
            task_id: 可选，关联已有的任务 ID
            dest_path: 可选，服务端目标子路径（如 "《逐玉》_a3f1/1.mp3"），
                       文件将存为 workspace/{task_id}/{dest_path}，
                       不传则直接放在任务目录根下

        返回:
            {"task_id": "...", "file_name": "...", "file_path": "...", "size": 123}
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        params = {}
        if task_id:
            params["task_id"] = task_id
        if dest_path:
            params["dest_path"] = dest_path

        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{self.base_url}/upload",
                files={"file": (file_path.name, f)},
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
        resp.raise_for_status()
        return resp.json()

    def upload_bytes(self, file_name: str, data: bytes,
                     task_id: Optional[str] = None) -> dict:
        """
        上传二进制数据到服务端

        参数:
            file_name: 文件名
            data: 文件内容
            task_id: 可选，关联已有的任务 ID

        返回:
            同 upload()
        """
        params = {}
        if task_id:
            params["task_id"] = task_id

        resp = requests.post(
            f"{self.base_url}/upload",
            files={"file": (file_name, data)},
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 执行脚本 ──────────────────────────────────────────

    def run(self, script: str, args: list[str] = [],
            task_id: Optional[str] = None,
            env: Optional[dict[str, str]] = None,
            client_mode: bool = False,
            video_summary: Optional[str] = None) -> dict:
        """
        远程执行脚本

        参数通过 JSON body 传递，避免 URL 长度限制（支持大批量输入）。

        参数:
            script: 脚本文件名，如 "audio_translate.py"
            args: 命令行参数列表
            task_id: 可选，关联已有的上传任务
            env: 可选，额外环境变量
            client_mode: 可选，是否为客户端模式（服务端据此调整行为）
            video_summary: 可选，视频名称摘要（用于 Web UI 展示）

        返回:
            {"task_id": "...", "script": "...", "status": "running"}
        """
        body = {
            "script": script,
            "args": args,
        }
        if task_id:
            body["task_id"] = task_id
        if env:
            body["env"] = env
        if client_mode:
            body["client_mode"] = True
        if video_summary:
            body["video_summary"] = video_summary

        resp = requests.post(
            f"{self.base_url}/run",
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 查询状态 ──────────────────────────────────────────

    def status(self, task_id: str, since_line: int = 0) -> dict:
        """
        查询任务状态

        参数:
            task_id: 任务 ID
            since_line: 增量日志起始行号，0 表示获取全部

        返回:
            {"task_id": "...", "status": "running|done|failed|cancelled",
             "return_code": 0, "stdout": "...", "stderr": "...",
             "total_lines": 123}
        """
        params = {}
        if since_line > 0:
            params["since_line"] = since_line

        resp = requests.get(
            f"{self.base_url}/status/{task_id}",
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 等待完成 ──────────────────────────────────────────

    def wait(self, task_id: str, poll_interval: float = 2.0,
             timeout: float = 900.0,
             network_retry_seconds: float = 60.0,
             on_progress: Optional[callable] = None) -> dict:
        """
        轮询等待任务完成

        超时策略：活跃度超时（非挂钟超时）。只要服务端持续有新输出，
        就不会超时；仅当连续 timeout 秒无新输出时才判定超时。
        这确保长时间任务（如 100+ 段 TTS 翻译）不会被挂钟上限误杀。

        网络容错：HTTP 请求出现瞬时网络故障（连接超时、断开、读超时等）时，
        不会立刻终止，而是打印警告并继续重试，直到累计连续失败时间超过
        network_retry_seconds 才放弃。这避免了客户端因短暂网络抖动
        （服务端仍在正常跑）就杀掉整个进程。

        参数:
            task_id: 任务 ID
            poll_interval: 轮询间隔（秒）
            timeout: 最大无输出空闲时间（秒），默认 900s（15 分钟）
            network_retry_seconds: 网络故障最大累计容忍时长（秒），
                                   默认 60s。期间任何一次成功都会清零计数。
            on_progress: 可选回调，每次轮询时调用，参数为 status dict

        返回:
            最终的 status dict

        异常:
            TimeoutError: 活跃度超时（长时间无新输出）或网络持续不通
            RuntimeError: 任务失败或被取消
            KeyboardInterrupt: 透传给上层（不会被捕获）
        """
        since_line = 0
        last_raw_total_lines = 0
        last_activity_time = time.time()
        # 网络抖动重试状态
        network_failure_start: float | None = None
        consecutive_failures = 0

        while True:
            try:
                info = self.status(task_id, since_line=since_line)
            except KeyboardInterrupt:
                # 上层负责处理 Ctrl+C，不在这里吞掉
                raise
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.HTTPError,
            ) as e:
                # 网络层瞬时故障：累计计时，超过阈值才放弃
                now = time.time()
                if network_failure_start is None:
                    network_failure_start = now
                consecutive_failures += 1
                elapsed = now - network_failure_start

                if elapsed > network_retry_seconds:
                    raise TimeoutError(
                        f"任务 {task_id} 网络持续不通已 {elapsed:.0f}s "
                        f"(连续 {consecutive_failures} 次失败)，最后错误: {e}"
                    )

                print(
                    f"  [网络抖动] 第 {consecutive_failures} 次失败 "
                    f"(累计 {elapsed:.0f}s/{network_retry_seconds:.0f}s)，"
                    f"将重连并继续轮询... ({type(e).__name__})"
                )
                # 网络抖动期间冻结活跃度时钟，不能把没收到响应当作"任务无输出"
                last_activity_time = now
                time.sleep(poll_interval)
                continue

            # 成功一次：重置网络失败计数
            if network_failure_start is not None:
                print(f"  [网络已恢复] 重连成功，继续轮询")
                network_failure_start = None
                consecutive_failures = 0

            if on_progress:
                on_progress(info)

            # since_line 基于 total_lines（过滤后的可见行数），用于增量 stdout 对齐
            total_lines = info.get("total_lines", 0)
            if total_lines > since_line:
                since_line = total_lines

            # 活跃度检测：基于 raw_total_lines（原始未过滤行数）
            # 因为 client_mode 下服务端会过滤 stdout，可见行数增长缓慢，
            # 但子进程仍在持续输出日志，raw_total_lines 能反映真实活跃状态
            raw_total_lines = info.get("raw_total_lines", total_lines)
            if raw_total_lines > last_raw_total_lines:
                last_activity_time = time.time()
                last_raw_total_lines = raw_total_lines

            if info["status"] in ("done", "failed", "cancelled"):
                if info["status"] != "done":
                    raise RuntimeError(
                        f"任务 {task_id} 状态: {info['status']}\n"
                        f"stderr: {info.get('stderr', '')}"
                    )
                return info

            # 活跃度超时：长时间无新输出
            idle_seconds = time.time() - last_activity_time
            if idle_seconds > timeout:
                raise TimeoutError(
                    f"任务 {task_id} 已 {timeout:.0f}s 无新输出 "
                    f"(状态: {info['status']}, 原始输出行数: {raw_total_lines})"
                )

            time.sleep(poll_interval)

    # ─── 下载结果 ──────────────────────────────────────────

    def download(self, task_id: str, remote_path: str,
                 local_path: Optional[str | Path] = None) -> Path:
        """
        下载任务结果文件

        参数:
            task_id: 任务 ID
            remote_path: 服务端文件路径（相对于任务工作目录）
            local_path: 本地保存路径，默认为当前目录下的文件名

        返回:
            本地文件路径
        """
        resp = requests.get(
            f"{self.base_url}/download/{task_id}/{remote_path}",
            headers=self._headers(),
            timeout=self.timeout,
            stream=True,
        )
        resp.raise_for_status()

        if local_path is None:
            # 从 URL 或 Content-Disposition 推断文件名
            local_path = Path(remote_path).name

        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return local_path

    # ─── 列出文件 ──────────────────────────────────────────

    def list_files(self, task_id: str, sub_dir: str = "",
                   since: float = 0, with_hash: bool = False) -> dict:
        """
        列出任务工作目录中的文件

        参数:
            task_id: 任务 ID
            sub_dir: 子目录路径
            since: 增量过滤，只返回 mtime > since 的文件，0 表示列出全部
            with_hash: 是否请求服务端返回文件 MD5 哈希（用于内容对比）

        返回:
            {"task_id": "...", "path": "...", "items": [...]}
            with_hash=True 时，文件项包含 "hash" 字段
        """
        params = {}
        if sub_dir:
            params["sub_dir"] = sub_dir
        if since > 0:
            params["since"] = since
        if with_hash:
            params["with_hash"] = "true"

        resp = requests.get(
            f"{self.base_url}/files/{task_id}",
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 服务端时间查询 ──────────────────────────────────────

    def get_server_time(self) -> dict:
        """获取服务端时间，用于客户端编辑重跑前的时间同步检查。

        返回:
            {"server_time": float, "timezone": str}
        """
        resp = requests.get(
            f"{self.base_url}/server-time",
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 批量删除文件 ──────────────────────────────────────

    def delete_files(self, task_id: str,
                     files: list[str] | None = None,
                     dirs: list[str] | None = None) -> dict:
        """批量删除任务工作目录中的文件/目录。

        参数:
            task_id: 任务 ID
            files: 要删除的文件相对路径列表
            dirs: 要删除的目录相对路径列表

        返回:
            {"task_id": "...", "deleted_files": [...], "deleted_dirs": [...], "errors": [...]}
        """
        body = {
            "files": files or [],
            "dirs": dirs or [],
        }
        resp = requests.post(
            f"{self.base_url}/delete/{task_id}",
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 取消任务 ──────────────────────────────────────────

    def cancel(self, task_id: str) -> dict:
        """取消正在运行的任务"""
        resp = requests.post(
            f"{self.base_url}/cancel/{task_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 归档任务到 US3 ────────────────────────────────────

    def archive_task(self, task_id: str, timeout: float = 1800.0) -> dict:
        """通知服务端将任务归档到 US3，上传后删除服务端本地文件。

        此方法应在客户端下载并校验完所有文件后调用，
        作为翻译任务的最后一个步骤。

        参数:
            task_id: 任务 ID
            timeout: 单次请求超时（秒），默认 1800（30 分钟），
                     大任务目录上传可能需要较长时间

        返回:
            {"task_id": "...", "status": "archived"|"failed", ...}
            status="archived" 表示上传成功且本地已删除
            status="failed" 表示上传失败，本地文件保留（local_preserved=True）

        异常:
            requests.HTTPError: 服务端返回 404（任务不存在）或 503（US3 未配置）
        """
        resp = requests.post(
            f"{self.base_url}/archive/{task_id}",
            headers=self._headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── 从 US3 恢复任务 ──────────────────────────────────

    def restore_task(self, task_id: str, timeout: float = 1800.0) -> dict | None:
        """从 US3 恢复任务到服务端本地工作目录。

        如果服务端本地已存在任务目录，直接返回（无需恢复）。
        如果 US3 上存在，下载到服务端本地。
        如果都不存在，返回 None。

        参数:
            task_id: 任务 ID
            timeout: 单次请求超时（秒），默认 1800（30 分钟）

        返回:
            恢复结果 dict，或 None（任务在本地和 US3 均不存在）

        异常:
            requests.HTTPError: 服务端返回 503（US3 未配置）或其他非 404 错误
        """
        try:
            resp = requests.post(
                f"{self.base_url}/restore/{task_id}",
                headers=self._headers(),
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    # ─── 便捷方法：上传 → 执行 → 等待 ──────────────────────

    def run_with_files(self, script: str, args: list[str],
                       files: list[str | Path],
                       poll_interval: float = 2.0,
                       timeout: float = 3600.0,
                       on_progress: Optional[callable] = None) -> dict:
        """
        一站式：上传文件 → 执行脚本 → 等待完成

        参数:
            script: 脚本名
            args: 命令行参数
            files: 要上传的本地文件路径列表
            poll_interval: 轮询间隔
            timeout: 最大等待时间
            on_progress: 进度回调

        返回:
            最终的 status dict
        """
        # 上传所有文件，共享同一个 task_id
        task_id = None
        for f in files:
            result = self.upload(f, task_id=task_id)
            task_id = result["task_id"]

        # 执行脚本
        self.run(script, args, task_id=task_id)

        # 等待完成
        return self.wait(task_id, poll_interval=poll_interval,
                         timeout=timeout, on_progress=on_progress)
