using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace CodexNativeAssist
{
    // Who is logged in to Codex. Only used when a caller injects one (tests, or an explicit future flow):
    // the recharge window asks the user for the account instead of reading Codex's login state.
    sealed class Account
    {
        public string Id, Email, Plan;
    }

    // The recharge binding (server-issued token + account id + base url). Kept per Windows user with DPAPI,
    // so a copied file is useless under another account. Nothing here touches Codex credentials.
    static class Vault
    {
        static string File_ { get { return Paths.In("binding.bin"); } }

        public static void Save(Dictionary<string, object> binding)
        {
            if (binding == null) { Clear(); return; }
            byte[] raw = Encoding.UTF8.GetBytes(Json.Stringify(binding));
            byte[] sealedBytes = ProtectedData.Protect(raw, null, DataProtectionScope.CurrentUser);
            Paths.WriteAtomic(File_, Convert.ToBase64String(sealedBytes));
            Log.Lifecycle("binding", Json.D("action", "saved", "base_url", Json.Str(binding, "base_url")));
        }

        public static Dictionary<string, object> Load()
        {
            try
            {
                if (!File.Exists(File_)) return null;
                byte[] sealedBytes = Convert.FromBase64String(System.IO.File.ReadAllText(File_, Encoding.UTF8).Trim());
                byte[] raw = ProtectedData.Unprotect(sealedBytes, null, DataProtectionScope.CurrentUser);
                return Json.ParseObject(Encoding.UTF8.GetString(raw));
            }
            catch (Exception ex)
            {
                // Written by another Windows user, or corrupted: that is simply no binding.
                Log.Lifecycle("binding", Json.D("action", "unreadable", "error", ex.GetType().Name));
                return null;
            }
        }

        public static void Clear()
        {
            try { if (System.IO.File.Exists(File_)) System.IO.File.Delete(File_); }
            catch (Exception) { }
            Log.Lifecycle("binding", Json.D("action", "cleared"));
        }
    }
}
