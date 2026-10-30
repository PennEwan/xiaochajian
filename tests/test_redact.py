import unittest

from ctr.redact import mentions_sensitive_file, redact_obj, redact_text


class RedactTextTest(unittest.TestCase):
    def assertRedacted(self, text, secret, kind):
        out, counts = redact_text(text)
        self.assertNotIn(secret, out)
        self.assertIn(f"[REDACTED:{kind}]", out)
        self.assertGreaterEqual(counts[kind], 1)

    def test_openai_key(self):
        key = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
        self.assertRedacted(f"use {key} here", key, "openai_key")

    def test_anthropic_key(self):
        key = "sk-ant-api03-" + "x" * 30
        self.assertRedacted(key, key, "anthropic_key")

    def test_github_token(self):
        tok = "ghp_" + "A" * 36
        self.assertRedacted(f"token={tok}", tok, "github_token")

    def test_aws_access_key(self):
        self.assertRedacted("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP", "aws_access_key")

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        self.assertRedacted(f"cookie {jwt}", jwt, "jwt")

    def test_bearer(self):
        self.assertRedacted("Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234", "auth_header")

    def test_pem_private_key(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        self.assertRedacted(f"x\n{pem}\ny", "MIIEowIBAAKCAQEA", "private_key")

    def test_url_credentials(self):
        out, counts = redact_text("git clone https://alice:hunter2pass@example.com/repo.git")
        self.assertNotIn("hunter2pass", out)
        self.assertIn("https://alice:[REDACTED:url_password]@example.com", out)
        self.assertEqual(counts["url_password"], 1)

    def test_env_assignment(self):
        self.assertRedacted("set OPENAI_API_KEY=abc123secretvalue", "abc123secretvalue", "assignment")
        self.assertRedacted('password: "s3cr3t!"', "s3cr3t!", "assignment")

    def test_curl_header_inline(self):
        self.assertRedacted('curl -H "x-api-key: zzzzzzzz1234" https://x', "zzzzzzzz1234", "auth_header")

    def test_recharge_command_is_dropped_whole(self):
        # `/充值` opens the CDK window; anything typed after it may be a CDK.
        for text in ["/充值", "  /充值 ABCD-EFGH-1234", "/充值ABCD-EFGH-1234\n多行", "／充值 ABCD-EFGH-1234"]:
            out, counts = redact_text(text)
            self.assertEqual(out, "[REDACTED:recharge_command]", text)
            self.assertEqual(counts["recharge_command"], 1)

    def test_leaves_ordinary_text_alone(self):
        for text in ["max_tokens: 100", "input_tokens=5321", "echo $OPENAI_API_KEY",
                     "def login(password: str):", "hello world", "skill-based task",
                     "怎么 /充值？", "充值失败了"]:
            out, counts = redact_text(text)
            self.assertEqual(out, text, text)
            self.assertEqual(sum(counts.values()), 0, text)


class CdkAndSessionTest(unittest.TestCase):
    """P3: CDKs and session credentials must never reach the sync backend."""

    def assertRedacted(self, text, secret, kind):
        out, counts = redact_text(text)
        self.assertNotIn(secret, out, text)
        self.assertIn(f"[REDACTED:{kind}]", out, text)
        self.assertGreaterEqual(counts[kind], 1, text)

    def test_cdk_after_keyword(self):
        cases = [("我的兑换码是 TEST-OK01", "TEST-OK01"), ("兑换码：abcd2345efgh", "abcd2345efgh"),
                 ("cdk=XYZW-9876", "XYZW-9876"), ("CDK: k3y-value-1", "k3y-value-1"),
                 ("redeem code ABCD-EFGH-IJKL-MNOP", "ABCD-EFGH-IJKL-MNOP"),
                 ("这张卡密 QWER1234ASDF", "QWER1234ASDF"), ("帮我查一下激活码ZXCV5678", "ZXCV5678"),
                 ("充值码为 a1b2c3d4", "a1b2c3d4")]
        for text, secret in cases:
            self.assertRedacted(text, secret, "cdk")

    def test_cdk_in_quotes_json_and_chinese_phrasing(self):
        secret = "ABCD1234EFGH"
        for text in [f'cdk="{secret}"', f'--cdk "{secret}"', f"curl -d '{{\"cdk\":\"{secret}\"}}'",
                     f"兑换码为：{secret}", f"兑换码是: {secret}", f"CDK码：{secret}", f"CDK：「{secret}」",
                     f"兑换码是“{secret}”", "兑换码 abcd1234efgh", f"cdkey={secret}"]:
            out, counts = redact_text(text)
            self.assertNotIn(secret.lower(), out.lower(), text)
            self.assertEqual(counts["cdk"], 1, text)

    def test_cdk_keys_in_any_case_style(self):
        out, _ = redact_obj({"cdkCode": "x1", "userCdk": "x2", "cdkey": "x3", "activationCode": "x4",
                             "redeem-code": "x5", "Redeem Code": "x6"})
        self.assertEqual(set(out.values()), {"[REDACTED:key]"})

    def test_placeholders_are_not_counted_twice(self):
        once, first = redact_obj({"password": "pw1", "cmd": "curl -H 'Authorization: Bearer abcdefghijklmnop1234' "
                                                             "https://u:secretpw@x/"})
        twice, second = redact_obj(once)
        self.assertEqual(twice, once)
        self.assertEqual(sum(second.values()), 0, second)

    def test_grouped_code_without_keyword(self):
        self.assertRedacted("try AB12-CD34-EF56-GH78 now", "AB12-CD34-EF56-GH78", "cdk_like")

    def test_session_and_app_tokens(self):
        self.assertRedacted("sess-" + "A1b2C3d4E5f6G7h8I9j0K1l2", "A1b2C3d4E5f6G7h8I9j0K1l2", "session_key")
        self.assertRedacted("token cdkb_" + "x" * 24, "x" * 24, "app_token")
        self.assertRedacted("Bearer-less ctrd_" + "Q" * 30, "Q" * 30, "app_token")

    def test_cdk_and_session_keys(self):
        out, counts = redact_obj({"cdk": "TEST-OK01", "redeem_code": "abc", "兑换码": "x1y2",
                                  "sessionid": "zzz", "session_id": "01a1634e-e806", "cdk_count": 3})
        self.assertEqual(out["cdk"], "[REDACTED:key]")
        self.assertEqual(out["redeem_code"], "[REDACTED:key]")
        self.assertEqual(out["兑换码"], "[REDACTED:key]")
        self.assertEqual(out["sessionid"], "[REDACTED:key]")
        self.assertEqual(out["session_id"], "01a1634e-e806", "Codex session ids are kept for linking")
        self.assertEqual(out["cdk_count"], 3)

    def test_ordinary_text_about_cdks_is_kept(self):
        for text in ["the CDK window opens", "CDK 充值插件", "fix the cdk redeem flow", "CDK API",
                     "uuid 550E8400-E29B-41D4-A716-446655440000", "ISO-8601 dates", "session_id: abc",
                     "AB-CD-EF", "SECTION-HEADER-TITLE-TEXT"]:
            out, counts = redact_text(text)
            self.assertEqual(out, text, text)
            self.assertEqual(sum(counts.values()), 0, text)


class RedactObjTest(unittest.TestCase):
    def test_sensitive_keys_only_for_strings(self):
        obj = {"api_key": "plainvalue", "password": "pw", "input_tokens": 12,
               "token_usage": {"total_tokens": 9}, "access_token": "abc", "name": "ok"}
        out, counts = redact_obj(obj)
        self.assertEqual(out["api_key"], "[REDACTED:key]")
        self.assertEqual(out["password"], "[REDACTED:key]")
        self.assertEqual(out["access_token"], "[REDACTED:key]")
        self.assertEqual(out["input_tokens"], 12)
        self.assertEqual(out["token_usage"], {"total_tokens": 9})
        self.assertEqual(out["name"], "ok")
        self.assertEqual(counts["key"], 3)

    def test_nested_lists_and_values(self):
        key = "sk-" + "Z" * 40
        out, counts = redact_obj({"a": [{"cmd": f"curl -d {key}"}]})
        self.assertNotIn(key, str(out))
        self.assertEqual(counts["openai_key"], 1)

    def test_does_not_mutate_input(self):
        obj = {"password": "x"}
        redact_obj(obj)
        self.assertEqual(obj, {"password": "x"})


class SensitiveFileTest(unittest.TestCase):
    def test_detects(self):
        for s in [r"type C:\Users\a\.codex\auth.json", "cat .env", "Get-Content id_rsa",
                  {"path": "D:/x/server.pem"}, "cat ~/.git-credentials"]:
            self.assertTrue(mentions_sensitive_file(s), s)

    def test_ignores(self):
        for s in ["cat README.md", "ls src", {"path": "environment.py"}, "authors.json"]:
            self.assertFalse(mentions_sensitive_file(s), s)


if __name__ == "__main__":
    unittest.main()
