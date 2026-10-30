using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace CodexNativeAssist
{
    // Minimal binding to winsqlite3.dll, the SQLite that ships with Windows 10/11 (System32).
    // Nothing to install or bundle. All strings cross the boundary as UTF-8.
    sealed class SqliteException : Exception
    {
        public readonly int Code;
        public SqliteException(int code, string message) : base(message) { Code = code; }
        public bool Busy { get { return (Code & 0xFF) == 5 || (Code & 0xFF) == 6; } }
    }

    sealed class Db : IDisposable
    {
        const string Lib = "winsqlite3.dll";
        const CallingConvention Cc = CallingConvention.StdCall; // winsqlite3 is built with __stdcall (x86); x64 has one convention

        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_open_v2(byte[] filename, out IntPtr db, int flags, IntPtr vfs);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_close_v2(IntPtr db);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_busy_timeout(IntPtr db, int ms);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_prepare_v2(IntPtr db, byte[] sql, int n, out IntPtr stmt, IntPtr tail);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_step(IntPtr stmt);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_finalize(IntPtr stmt);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_bind_text(IntPtr stmt, int i, byte[] v, int n, IntPtr destructor);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_bind_int64(IntPtr stmt, int i, long v);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_bind_null(IntPtr stmt, int i);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_column_count(IntPtr stmt);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_column_type(IntPtr stmt, int i);
        [DllImport(Lib, CallingConvention = Cc)] static extern long sqlite3_column_int64(IntPtr stmt, int i);
        [DllImport(Lib, CallingConvention = Cc)] static extern IntPtr sqlite3_column_text(IntPtr stmt, int i);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_column_bytes(IntPtr stmt, int i);
        [DllImport(Lib, CallingConvention = Cc)] static extern IntPtr sqlite3_column_name(IntPtr stmt, int i);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_changes(IntPtr db);
        [DllImport(Lib, CallingConvention = Cc)] static extern IntPtr sqlite3_errmsg(IntPtr db);
        [DllImport(Lib, CallingConvention = Cc)] static extern int sqlite3_extended_result_codes(IntPtr db, int onoff);

        static readonly IntPtr Transient = new IntPtr(-1);
        IntPtr _db;
        int _depth; // nested Write() calls share the outer transaction

        public Db(string path, int busyMs)
        {
            int rc = sqlite3_open_v2(Utf8(path), out _db, 0x2 | 0x4 | 0x10000 /* READWRITE|CREATE|FULLMUTEX */, IntPtr.Zero);
            if (rc != 0) { string m = Error(); sqlite3_close_v2(_db); throw new SqliteException(rc, "open: " + m); }
            sqlite3_extended_result_codes(_db, 1);
            sqlite3_busy_timeout(_db, busyMs);
        }

        public void Dispose()
        {
            if (_db != IntPtr.Zero) { sqlite3_close_v2(_db); _db = IntPtr.Zero; }
        }

        static byte[] Utf8(string s)
        {
            var b = Encoding.UTF8.GetBytes(s);
            Array.Resize(ref b, b.Length + 1); // NUL-terminated
            return b;
        }

        string Error() { return Marshal.PtrToStringAnsi(sqlite3_errmsg(_db)) ?? "?"; }

        IntPtr Prepare(string sql, object[] args)
        {
            IntPtr st;
            byte[] q = Utf8(sql);
            int rc = sqlite3_prepare_v2(_db, q, q.Length, out st, IntPtr.Zero);
            if (rc != 0) throw new SqliteException(rc, Error() + " in: " + sql);
            for (int i = 0; i < args.Length; i++)
            {
                object a = args[i];
                if (a == null) sqlite3_bind_null(st, i + 1);
                else if (a is string) { byte[] b = Encoding.UTF8.GetBytes((string)a); sqlite3_bind_text(st, i + 1, b, b.Length, Transient); }
                else if (a is bool) sqlite3_bind_int64(st, i + 1, (bool)a ? 1 : 0);
                else sqlite3_bind_int64(st, i + 1, Convert.ToInt64(a));
            }
            return st;
        }

        public int Exec(string sql, params object[] args)
        {
            IntPtr st = Prepare(sql, args);
            try
            {
                int rc;
                while ((rc = sqlite3_step(st)) == 100) { }
                if (rc != 101) throw new SqliteException(rc, Error());
                return sqlite3_changes(_db);
            }
            finally { sqlite3_finalize(st); }
        }

        public List<Dictionary<string, object>> Query(string sql, params object[] args)
        {
            var rows = new List<Dictionary<string, object>>();
            IntPtr st = Prepare(sql, args);
            try
            {
                int rc, n = sqlite3_column_count(st);
                while ((rc = sqlite3_step(st)) == 100)
                {
                    var row = new Dictionary<string, object>();
                    for (int i = 0; i < n; i++)
                    {
                        string name = Marshal.PtrToStringAnsi(sqlite3_column_name(st, i));
                        switch (sqlite3_column_type(st, i))
                        {
                            case 1: row[name] = sqlite3_column_int64(st, i); break;
                            case 5: row[name] = null; break;
                            default:
                                int len = sqlite3_column_bytes(st, i);
                                IntPtr p = sqlite3_column_text(st, i);
                                var buf = new byte[len];
                                if (len > 0) Marshal.Copy(p, buf, 0, len);
                                row[name] = Encoding.UTF8.GetString(buf);
                                break;
                        }
                    }
                    rows.Add(row);
                }
                if (rc != 101) throw new SqliteException(rc, Error());
            }
            finally { sqlite3_finalize(st); }
            return rows;
        }

        public Dictionary<string, object> One(string sql, params object[] args)
        {
            var rows = Query(sql, args);
            return rows.Count > 0 ? rows[0] : null;
        }

        // BEGIN IMMEDIATE takes the write lock up front, so two writers never interleave and a
        // check made inside the transaction (consent still on, offset unchanged) holds until COMMIT.
        public T Write<T>(Func<T> body)
        {
            if (_depth > 0) return body();
            for (int attempt = 0; ; attempt++)
            {
                try { Exec("BEGIN IMMEDIATE"); break; }
                catch (SqliteException ex)
                {
                    if (!ex.Busy || attempt >= 50) throw;
                    Thread.Sleep(50); // busy_timeout already waited; try again a little later
                }
            }
            _depth++;
            try
            {
                T result = body();
                Exec("COMMIT");
                return result;
            }
            catch
            {
                try { Exec("ROLLBACK"); } catch (SqliteException) { }
                throw;
            }
            finally { _depth--; }
        }

        public void Write(Action body)
        {
            Write<object>(delegate { body(); return null; });
        }
    }
}
