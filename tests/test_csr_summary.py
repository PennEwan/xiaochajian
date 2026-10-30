import unittest

from csr import scanner, summary

from csr_helpers import SID, Rollout
from test_csr_scanner import ScanCase, three_prompts


def busy_session() -> Rollout:
    r = three_prompts()
    r.start("t4").prompt("t4", "会失败").complete("t4", error={"message": "busy",
                                                              "codex_error_info": "server_overloaded"})
    r.start("t5").prompt("t5", "被打断")
    r.tool("t5", "sleep 100", with_result=False)
    r.abort("t5")
    r.start("t6").prompt("t6", "还在跑")
    return r


class StatsTest(ScanCase):
    def setUp(self):
        super().setUp()
        data = busy_session().data()
        lines = data.split(b"\n")
        del lines[5]  # drop one record: one ordinal gap
        self.write(b"\n".join(lines[:8]) + b"\n{bad\n" + b"\n".join(lines[8:]))
        scanner.scan(self.storage, self.sessions)
        self.s = summary.session_stats(self.storage, SID)

    def test_counts(self):
        s = self.s
        self.assertEqual(s["prompts"], 5)  # t1's prompt line was dropped
        self.assertEqual(s["turns"], {"started": 6, "completed": 3, "failed": 1, "aborted": 1, "open": 1})
        self.assertEqual(s["replies"], {"total": 4, "final_answer": 3, "commentary": 1, "other": 0})
        self.assertEqual(s["tool_calls"]["model_level"], 3)
        self.assertEqual(s["tool_calls"]["without_result"], 1)
        self.assertEqual(s["tool_calls"]["executions"], 3)
        self.assertEqual(s["tool_calls"]["execution_failed"], 1)
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["parse_failures"], 1)
        self.assertEqual(s["codex_version"], "0.163.0-alpha.10.2")
        self.assertEqual(s["originator"], "Codex Desktop")

    def test_models_in_order_of_use(self):
        self.assertEqual([(m["model"], m["turns"]) for m in self.s["models"]],
                         [("gpt-6-luna", 5), ("gpt-6.1-sol", 1)])
        self.assertEqual(self.s["model_switches"], 2)

    def test_missing(self):
        m = self.s["missing"]
        self.assertEqual(m["ordinal_gap_records"], 1)
        self.assertEqual(m["tool_calls_without_result"], 1)
        self.assertEqual(m["open_turns"], 1)
        self.assertEqual(m["missing_fields"], {"summary": 2})

    def test_tokens_are_the_last_thread_total(self):
        self.assertEqual(self.s["tokens"]["total_tokens"], 4000)

    def test_current_session_is_the_most_recently_updated(self):
        self.assertEqual(summary.current_session(self.storage), SID)


class CoverageTest(ScanCase):
    def test_rows_report_applicable_and_present_counts(self):
        self.write(busy_session().data())
        scanner.scan(self.storage, self.sessions)
        rows = {r["field"]: r for r in summary.coverage(self.storage, [SID])}
        self.assertEqual((rows["用户 Prompt"]["applicable"], rows["用户 Prompt"]["present"]), (6, 6))
        self.assertEqual((rows["公开推理摘要"]["applicable"], rows["公开推理摘要"]["present"]), (3, 1))
        self.assertEqual((rows["工具调用结果"]["applicable"], rows["工具调用结果"]["present"]), (3, 2))
        self.assertEqual((rows["回合结束事件"]["applicable"], rows["回合结束事件"]["present"]), (6, 5))
        self.assertEqual((rows["模型名称"]["applicable"], rows["模型名称"]["present"]), (6, 6))
        self.assertIn("推断", rows["Turn ID"]["note"])
        table = summary.coverage_markdown(list(rows.values()))
        self.assertTrue(table.startswith("| 字段 |"))


if __name__ == "__main__":
    unittest.main()
