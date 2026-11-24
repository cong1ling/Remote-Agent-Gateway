from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from gateway.profiles import validate_profile_declarations, validate_profile_result
from remote_agent.profiles import build_profile_handlers
from remote_agent.profiles.system_observe_v1 import common
from remote_agent.profiles.system_observe_v1.actions import host_info, network, processes


_TCP4_HEADER = b"  sl  local_address rem_address   st tx_queue rx_queue\n"
_TCP6_HEADER = b"  sl  local_address remote_address   st tx_queue rx_queue\n"
_MACOS_TCP_HEADER = (
    b"Active Internet connections (including servers)\n"
    b"Proto Recv-Q Send-Q Local Address Foreign Address (state)\n"
)


class SystemObserveProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_observation_actions_are_read_only_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handler = build_profile_handlers(
                ["system_observe.v1"],
                Path(directory),
            )["system_observe.v1"]
            self.assertEqual(
                set(handler.actions),
                {"get_host_info", "get_resources", "get_processes", "get_network"},
            )
            for action in handler.actions:
                result = await handler.execute(action, f"request-{action}", {})
                validated = validate_profile_result("system_observe.v1", action, result)
                self.assertTrue(validated["observed_at"].endswith("Z"))
                with self.assertRaises(ValueError):
                    await handler.execute(
                        action,
                        f"invalid-{action}",
                        {"command": "whoami"},
                    )

    async def test_linux_host_reports_distribution_and_tool_presence_not_permissions(self) -> None:
        os_release = b'ID=ubuntu\nVERSION_ID="24.04"\nNAME="not included"\nURL="https://example.invalid/"\n'
        with (
            patch.object(host_info, "platform_id", return_value="linux"),
            patch.object(host_info.os, "geteuid", return_value=1000, create=True),
            patch.object(host_info, "read_bounded", return_value=os_release) as read_release,
            patch.object(host_info.shutil, "which", side_effect=["/usr/bin/sudo", "/usr/bin/docker", None]) as which,
        ):
            result = await host_info.HostInfoAction().execute("host", {})

        self.assertEqual(result["privileges"], {"is_admin": None, "is_root": False})
        self.assertEqual(result["linux_distribution"], {"id": "ubuntu", "version_id": "24.04"})
        self.assertEqual(
            result["linux_tools"],
            {"sudo_on_path": True, "docker_on_path": True, "podman_on_path": False},
        )
        read_release.assert_called_once_with(Path("/etc/os-release"), 16 * 1024)
        self.assertEqual(which.call_args_list, [call("sudo"), call("docker"), call("podman")])
        self.assertEqual(validate_profile_result("system_observe.v1", "get_host_info", result), result)

    def test_linux_distribution_falls_back_only_for_missing_primary_file(self) -> None:
        with patch.object(host_info, "read_bounded", side_effect=[FileNotFoundError, b"ID=debian\n"]) as read_release:
            self.assertEqual(host_info._linux_distribution(), {"id": "debian", "version_id": None})
        self.assertEqual(read_release.call_args_list, [
            call(Path("/etc/os-release"), 16 * 1024),
            call(Path("/usr/lib/os-release"), 16 * 1024),
        ])
        for invalid_release in (PermissionError(), None, b"ID=some/credential\n", b"ID=" + b"x" * 65):
            with self.subTest(invalid_release=invalid_release):
                effect = [invalid_release]
                with patch.object(host_info, "read_bounded", side_effect=effect):
                    self.assertIsNone(host_info._linux_distribution())

    def test_current_privileges_distinguish_false_from_unknown(self) -> None:
        for value in (False, True, None):
            with patch.object(host_info, "current_windows_admin", return_value=value):
                self.assertEqual(host_info._current_privileges("windows"), {"is_admin": value, "is_root": None})
        with patch.object(host_info.os, "geteuid", return_value=0, create=True):
            self.assertEqual(host_info._current_privileges("macos"), {"is_admin": None, "is_root": True})
        with patch.object(host_info.os, "geteuid", side_effect=OSError, create=True):
            self.assertEqual(host_info._current_privileges("linux"), {"is_admin": None, "is_root": None})

    def test_macos_memory_uses_sample_page_size_and_physical_compressor_pages(self) -> None:
        for page_size in (4096, 16384):
            with self.subTest(page_size=page_size):
                total = 1000 * page_size
                vm_stat = (
                    f"Mach Virtual Memory Statistics: (page size of {page_size} bytes)\n".encode()
                    + b"Pages wired down:            100.\n"
                    b"Anonymous pages:              450.\n"
                    b"File-backed pages:            200.\n"
                    b"Pages stored in compressor:  1200.\n"
                    b"Pages occupied by compressor: 50.\n"
                )
                with (
                    patch.object(common, "platform_id", return_value="macos"),
                    patch.object(common, "run_macos_probe", side_effect=[f"{total}\n".encode(), vm_stat]) as probe,
                ):
                    self.assertEqual(common.memory_snapshot(), {
                        "total_bytes": total, "available_bytes": 400 * page_size,
                        "used_bytes": 600 * page_size, "utilization_percent": 60,
                    })
                self.assertEqual(probe.call_args_list, [
                    call(("/usr/sbin/sysctl", "-n", "hw.memsize"), 64),
                    call(("/usr/bin/vm_stat",), 8192),
                ])

    def test_macos_incomplete_or_impossible_memory_sample_has_no_utilization(self) -> None:
        header = b"Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        invalid_samples = (
            None,
            b"vm_stat: failed to get statistics\n",
            header + b"Pages wired down: 100.\nAnonymous pages: 450.\n",
            header + b"Pages wired down: 1000.\nAnonymous pages: 450.\nPages occupied by compressor: 50.\n",
        )
        for sample in invalid_samples:
            with self.subTest(sample=sample), patch.object(
                common, "run_macos_probe", side_effect=[b"16384000\n", sample],
            ):
                self.assertEqual(common._macos_memory_snapshot(), {"total_bytes": 16384000})
        with patch.object(common, "run_macos_probe", return_value=b"0\n") as probe:
            self.assertIsNone(common._macos_memory_snapshot())
            probe.assert_called_once()

    async def test_linux_listeners_are_local_deduplicated_and_bounded(self) -> None:
        ipv4 = _TCP4_HEADER + b"".join(
            f"{index}: 00000000:{port:04X} 00000000:0000 0A\n".encode()
            for index, port in enumerate(range(3000, 3130))
        )
        ipv4 += b"130: 0100007F:122B 0200007F:0022 01\n"
        ipv6 = (
            _TCP6_HEADER
            + b"0: 00000000000000000000000000000000:0BB8 00000000000000000000000000000000:0000 0A\n"
            b"1: 00000000000000000000000000000000:0050 00000000000000000000000000000000:0000 0A\n"
        )
        with (
            patch.object(network, "platform_id", return_value="linux"),
            patch.object(network, "_posix_interface_addresses", return_value={"127.0.0.1", "::1"}),
            patch.object(network, "read_bounded", side_effect=[ipv4, ipv6]) as read_table,
        ):
            result = await network.NetworkAction().execute("network", {})

        self.assertEqual(result["tcp_listeners"], {"ports": [80, *range(3000, 3127)], "truncated": True})
        self.assertEqual(read_table.call_args_list, [
            call(Path("/proc/net/tcp"), 1024 * 1024),
            call(Path("/proc/net/tcp6"), 1024 * 1024),
        ])
        self.assertEqual(validate_profile_result("system_observe.v1", "get_network", result), result)

    def test_linux_empty_tables_differ_from_missing_malformed_or_capped_tables(self) -> None:
        with patch.object(network, "read_bounded", side_effect=[_TCP4_HEADER, _TCP6_HEADER]):
            self.assertEqual(network._linux_listening_ports(), set())
        for invalid_table in (None, PermissionError(), FileNotFoundError(), b"truncated", _TCP6_HEADER + b"bad\n"):
            with self.subTest(table=invalid_table), patch.object(
                network, "read_bounded", side_effect=[_TCP4_HEADER, invalid_table],
            ):
                self.assertIsNone(network._linux_listening_ports())

    def test_macos_listeners_need_a_complete_numeric_snapshot(self) -> None:
        established = b"tcp4 0 0 192.0.2.15.14478 192.0.2.16.443 ESTABLISHED\n"
        listeners = b"tcp46 0 0 *.8080 *.* LISTEN\n" b"tcp6 0 0 ::1.5432 *.* LISTEN\n"
        cases = (
            (_MACOS_TCP_HEADER + established + listeners, {8080, 5432}),
            (_MACOS_TCP_HEADER + established, set()),
            (_MACOS_TCP_HEADER + listeners + b"Some tcp sockets may have been created.\n", None),
            (_MACOS_TCP_HEADER + b"tcp4 0 0 *.http *.* LISTEN\n", None),
            (b"", None),
            (None, None),
        )
        for sample, expected in cases:
            with self.subTest(sample=sample), patch.object(network, "run_macos_probe", return_value=sample) as probe:
                self.assertEqual(network._macos_listening_ports(), expected)
                probe.assert_called_once_with(("/usr/sbin/netstat", "-an", "-p", "tcp"), 1024 * 1024)

    def test_posix_process_observation_keeps_names_with_spaces(self) -> None:
        command = ["ps", "-eo", "pid=,ppid=,stat=,%cpu=,%mem=,comm="]
        sample = subprocess.CompletedProcess(command, 0, stdout=(
            b"120 100 S 0.2 0.5 Idle Worker\n"
            b"121 100 R+ 1.1 2.5 /Applications/Example App.app/Contents/MacOS/Example App\n"
        ))
        with (
            patch.object(processes.os, "name", "posix"),
            patch.object(processes.subprocess, "run", return_value=sample) as run,
        ):
            result = processes._collect_processes()
        self.assertEqual(run.call_args.args, (command,))
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertTrue(result["available"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["processes"], [
            {"pid": 120, "ppid": 100, "state": "S", "name": "Idle Worker",
             "cpu_percent": 0.2, "memory_percent": 0.5},
            {"pid": 121, "ppid": 100, "state": "R+", "name": "/Applications/Example App.app/Contents/MacOS/Example App",
             "cpu_percent": 1.1, "memory_percent": 2.5},
        ])
        self.assertEqual(validate_profile_result("system_observe.v1", "get_processes", result), result)

    @unittest.skipUnless(sys.platform == "darwin", "requires native macOS health observations")
    async def test_native_macos_reports_resources_processes_and_a_local_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            handler = build_profile_handlers(["system_observe.v1"], Path(directory))["system_observe.v1"]
            observations = {
                action: validate_profile_result(
                    "system_observe.v1", action, await handler.execute(action, f"macos-{action}", {}),
                )
                for action in handler.actions
            }

        host = observations["get_host_info"]
        self.assertEqual(host["os"], "macos")
        self.assertIn(host["architecture"], {"arm64", "x86_64"})
        self.assertEqual(host["privileges"]["is_root"], os.geteuid() == 0)
        resources = observations["get_resources"]
        self.assertGreater(resources["cpu"]["logical_count"], 0)
        self.assertEqual(len(resources["cpu"]["load_average"]), 3)
        self.assertGreater(resources["disks"][0]["total_bytes"], 0)
        self.assertIsNotNone(resources["memory"])
        self.assertGreater(resources["memory"]["total_bytes"], 0)
        self.assertIn("utilization_percent", resources["memory"])
        self.assertTrue(observations["get_processes"]["available"])
        self.assertTrue(observations["get_processes"]["processes"])
        network_snapshot = observations["get_network"]
        self.assertIn("127.0.0.1", network_snapshot["addresses"])
        self.assertIsNotNone(network_snapshot["tcp_listeners"])
        self.assertIn(port, network_snapshot["tcp_listeners"]["ports"])

    def test_get_network_does_not_resolve_names_or_open_sockets(self) -> None:
        with (
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS is not a local snapshot")),
            patch.object(socket, "getnameinfo", side_effect=AssertionError("reverse DNS is not allowed")),
            patch.object(socket, "socket", side_effect=AssertionError("socket probing is not allowed")),
        ):
            result = network._collect_network()
        self.assertIn("tcp_listeners", result)
        self.assertEqual(validate_profile_result("system_observe.v1", "get_network", result), result)

    @unittest.skipUnless(os.name == "nt", "requires the real Windows token and IP Helper API")
    async def test_native_windows_reports_a_local_tcp_listener_and_token(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            result = await network.NetworkAction().execute("network", {})

        self.assertIn("127.0.0.1", result["addresses"])
        self.assertIsNotNone(result["tcp_listeners"])
        self.assertIn(port, result["tcp_listeners"]["ports"])
        host = await host_info.HostInfoAction().execute("host", {})
        self.assertIsInstance(host["privileges"]["is_admin"], bool)
        self.assertIsNone(host["privileges"]["is_root"])

    def test_runtime_and_result_contracts_are_normalized(self) -> None:
        declarations = validate_profile_declarations(
            [
                {
                    "id": "system_observe.v1",
                    "runtime": {
                        "scope": "host",
                        "mode": "read_only",
                        "platform": "linux",
                        "actions": [
                            "get_host_info",
                            "get_resources",
                            "get_processes",
                            "get_network",
                        ],
                    },
                }
            ]
        )
        self.assertEqual(declarations[0]["runtime"]["mode"], "read_only")
        with self.assertRaisesRegex(ValueError, "read_only"):
            validate_profile_declarations(
                [
                    {
                        "id": "system_observe.v1",
                        "runtime": {
                            "scope": "host",
                            "mode": "managed",
                            "platform": "linux",
                            "actions": ["get_host_info"],
                        },
                    }
                ]
            )

    def test_result_validation_rejects_unbounded_processes(self) -> None:
        result = {
            "observed_at": "2026-11-22T00:00:00.000Z",
            "observed_at_unix": 1.0,
            "available": True,
            "processes": [{"pid": index, "name": "process"} for index in range(101)],
            "truncated": True,
        }
        with self.assertRaisesRegex(ValueError, "processes"):
            validate_profile_result("system_observe.v1", "get_processes", result)

    def test_result_validation_preserves_legacy_and_explicit_unknown_records(self) -> None:
        metadata = {"observed_at": "2026-11-22T00:00:00.000Z", "observed_at_unix": 1.0}
        legacy_host = {
            **metadata, "os": "linux", "system": "Linux", "release": "6.8",
            "hostname": "host", "version": "kernel", "architecture": "x86_64", "python": "3.11", "pid": 1,
        }
        legacy_network = {**metadata, "hostname": "host", "interfaces": [], "addresses": []}
        legacy_memory = {**metadata, "cpu": {}, "disks": [], "memory": {"total_bytes": 1}}
        for action, result in (
            ("get_host_info", legacy_host),
            ("get_host_info", {**legacy_host, "privileges": {"is_admin": None, "is_root": None}}),
            ("get_host_info", {**legacy_host, "linux_distribution": None}),
            ("get_resources", legacy_memory),
            ("get_resources", {**legacy_memory, "memory": None}),
            ("get_network", legacy_network),
            ("get_network", {**legacy_network, "tcp_listeners": None}),
            ("get_network", {**legacy_network, "tcp_listeners": {"ports": [], "truncated": False}}),
        ):
            with self.subTest(action=action, result=result):
                self.assertEqual(validate_profile_result("system_observe.v1", action, result), result)

    def test_result_validation_rejects_invalid_optional_metadata(self) -> None:
        legacy_host = {
            "observed_at": "2026-11-22T00:00:00.000Z", "observed_at_unix": 1.0,
            "os": "linux", "system": "Linux", "release": "6.8", "hostname": "host",
            "version": "kernel", "architecture": "x86_64", "python": "3.11", "pid": 1,
        }
        for field, value in (
            ("privileges", {"is_admin": "root", "is_root": None}),
            ("privileges", {"is_admin": True, "is_root": None, "raw_token": "not metadata"}),
            ("linux_distribution", {"id": "ubuntu", "version_id": "a" * 65}),
            ("linux_distribution", {"id": "../ubuntu", "version_id": None}),
            ("linux_tools", {"sudo_on_path": True, "docker_on_path": "/usr/bin/docker", "podman_on_path": False}),
        ):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                validate_profile_result("system_observe.v1", "get_host_info", {**legacy_host, field: value})

        legacy_network = {
            "observed_at": legacy_host["observed_at"], "observed_at_unix": legacy_host["observed_at_unix"],
            "hostname": "host", "interfaces": [], "addresses": [],
        }
        for invalid in (
            {"ports": list(range(1, 130)), "truncated": True},
            {"ports": [1, 1], "truncated": False},
            {"ports": [True, 443], "truncated": False},
            {"ports": [0, 65536], "truncated": False},
            {"ports": [443], "truncated": 0},
            {"ports": [], "truncated": True},
            {"ports": [], "truncated": False, "peers": ["192.0.2.1"]},
        ):
            with self.subTest(listeners=invalid), self.assertRaisesRegex(ValueError, "tcp_listeners"):
                validate_profile_result("system_observe.v1", "get_network", {**legacy_network, "tcp_listeners": invalid})


if __name__ == "__main__":
    unittest.main()
