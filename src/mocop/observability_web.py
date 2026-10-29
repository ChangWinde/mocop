"""Optional observer dispatch after shared HTTP authentication and validation."""

from http import HTTPStatus

from .api_describe import describe_meta
from .storage_analysis import handle_read as storage_read
from .terminal import RESPONSE_CODES
from .zenith_monitor import TrainingMonitor
from .zenith_monitor import handle_read as training_read


def attach_sources(server, terminal, user_memory, directory_disk, training_monitor):
    server.terminal, server.user_memory, server.directory_disk = (
        terminal,
        user_memory,
        directory_disk,
    )
    server.training_monitor = (
        training_monitor
        if training_monitor is not None
        else TrainingMonitor(start=False)
    )


def handle_read(handler, request_url):
    return training_read(handler, request_url) or storage_read(handler, request_url)


def send_meta(handler):
    server = handler.monitor_server
    metadata = describe_meta(
        restart_supported=server.restart is not None,
        manual_probe_supported=server.probe_control is not None,
        configuration_write_supported=handler._configuration_write_supported(),
        update_supported=server.updates is not None,
        authentication=server.authentication,
    )
    metadata["capabilities"].update(
        trainingMonitoringSupported=True,
        terminalSupported=server.terminal is not None,
        terminalApps=list(server.terminal.apps) if server.terminal else [],
    )
    handler._send_json(metadata)


def open_terminal(self, body: dict[str, object]) -> None:
    launcher = self.monitor_server.terminal
    if launcher is None:
        self._send_error(
            "local terminal launching is unavailable",
            HTTPStatus.SERVICE_UNAVAILABLE,
            code="SERVICE_UNAVAILABLE",
        )
        return
    result = launcher.open(str(body["host"]), str(body["app"]))
    response_status, code = RESPONSE_CODES.get(
        str(result.get("status")), (HTTPStatus.OK, None)
    )
    if code is not None:
        result = {**result, "code": code}
    self._send_json(result, response_status)
