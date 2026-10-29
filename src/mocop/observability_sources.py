"""Independent training, storage and user-memory source lifecycle."""

from .storage_analysis import PersonalStorageMonitor
from .terminal import TerminalLauncher
from .user_memory import DailyUserMemory
from .zenith_monitor import TrainingMonitor


def create_sources(config, state):
    def hosts():
        return [
            str(entry["host"]) for entry in state.snapshot_view().get("servers", ())
        ]

    return {
        "terminal": TerminalLauncher.detect(
            hosts, config.ssh_config, local_host=config.local_host
        ),
        "user_memory": DailyUserMemory(config, hosts),
        "directory_disk": PersonalStorageMonitor(config, hosts),
        "training_monitor": TrainingMonitor(),
    }


def start_sources(sources):
    for key in ("user_memory", "directory_disk"):
        sources[key].start()


def stop_sources(sources):
    if "training_monitor" in sources:
        sources["training_monitor"].stop.set()
    for key in ("user_memory", "directory_disk"):
        if key in sources:
            sources[key].stop()
