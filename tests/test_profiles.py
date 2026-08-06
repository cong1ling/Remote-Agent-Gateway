from __future__ import annotations

import unittest

from gateway.profiles import (
    validate_profile_declarations,
    validate_profile_result,
)


class ProfileContractTests(unittest.TestCase):
    def test_shell_declaration_requires_known_runtime_dialect(self) -> None:
        declarations = validate_profile_declarations(
            [
                {
                    "id": "shell.v1",
                    "runtime": {
                        "os": "windows",
                        "dialect": "powershell",
                        "executable": "powershell.exe",
                    },
                }
            ]
        )
        self.assertEqual(declarations[0]["runtime"]["dialect"], "powershell")

        with self.assertRaisesRegex(ValueError, "格式已升级"):
            validate_profile_declarations(["shell.v1"])
        with self.assertRaisesRegex(ValueError, "不匹配"):
            validate_profile_declarations(
                [
                    {
                        "id": "shell.v1",
                        "runtime": {
                            "os": "linux",
                            "dialect": "powershell",
                            "executable": "pwsh",
                        },
                    }
                ]
            )

    def test_shell_result_must_match_contract(self) -> None:
        result = validate_profile_result(
            "shell.v1",
            "exec",
            {"stdout": "ok", "stderr": "", "exit_code": 0},
        )
        self.assertEqual(result["exit_code"], 0)

        with self.assertRaisesRegex(ValueError, "exit_code"):
            validate_profile_result(
                "shell.v1",
                "exec",
                {"stdout": "ok", "stderr": "", "exit_code": "0"},
            )


if __name__ == "__main__":
    unittest.main()
