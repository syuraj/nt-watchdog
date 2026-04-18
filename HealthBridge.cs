#region Using declarations
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Diagnostics;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.AddOns;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    /// <summary>
    /// HealthBridge - HTTP API for external control of NT8.
    /// Exposes localhost on configurable port with JSON endpoints so external scripts
    /// can query account state, positions, and strategies without GUI automation.
    ///
    /// Starter endpoints:
    ///   GET /health       - liveness check
    ///   GET /accounts     - all accounts with cash, realized/unrealized P&L
    ///   GET /positions    - all open positions across all accounts
    ///   GET /connections  - broker connection states
    ///
    /// Extend by adding cases in Dispatch() and matching handler methods.
    /// </summary>
    public class HealthBridge : AddOnBase
    {
        private HttpListener _listener;
        private CancellationTokenSource _cts;
        private Task _serverTask;
        private Thread _workerThread;
        private Thread _heartbeatThread;
        private volatile bool _heartbeatRunning;
        private const int DefaultPort = 8899;
        private readonly string _listenUrl = BuildListenUrl();
        // Bump BuildId whenever editing HealthBridge.cs so the client can detect whether
        // NT is running the freshly-compiled DLL or a stale in-memory AddOn instance.
        // Format: UTC timestamp at edit time.
        private const string BuildId = "2026-04-18T18:45:00Z";
        private static readonly long _startedUtcTicks = DateTime.UtcNow.Ticks;
        private static long _lastRequestUtcTicks = DateTime.UtcNow.Ticks;
        private static long _lastMainThreadTickUtcTicks = DateTime.UtcNow.Ticks;
        private static int _lastMainThreadPingMs = -1;
        private static string _lastMainThreadError = "";
        private static long _lastRecoveryAttemptUtcTicks = 0;
        private static string _lastRecoveryAction = "none";
        private static string _lastRecoveryResult = "none";
        private static string _lastRecoveryError = "";

        // ────── Backtest job queue ──────
        private static readonly ConcurrentDictionary<string, BacktestJob> _jobs =
            new ConcurrentDictionary<string, BacktestJob>();
        private static readonly BlockingCollection<BacktestJob> _jobQueue =
            new BlockingCollection<BacktestJob>();
        private static readonly JavaScriptSerializer _jsonSer = new JavaScriptSerializer();

        private class BacktestRequest
        {
            public string strategy_name { get; set; }
            public string instrument { get; set; }           // e.g. "ES 03-26" or "ES ##-##" continuous
            public string bars_period_type { get; set; }     // "Minute", "Day", "Tick", "Second", "Week", "Month"
            public int bars_period_value { get; set; }       // 5 for 5-minute, 1 for 1-day, etc.
            public string from_date { get; set; }            // yyyy-MM-dd
            public string to_date { get; set; }              // yyyy-MM-dd
            public Dictionary<string, object> parameters { get; set; } // optional strategy params
        }

        private class BacktestJob
        {
            public string id;
            public BacktestRequest request;
            public string status;       // queued | running | completed | failed
            public string results_json; // populated when completed
            public string error;        // populated when failed
            public DateTime created;
            public DateTime? completed;
            public double elapsed_ms;
        }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "HealthBridge";
                Description = "HTTP API bridge for external watchdog tools";
            }
            else if (State == State.Active)
            {
                Log("state active - starting HealthBridge services");
                StartServer();
                StartMainThreadHeartbeat();
            }
            else if (State == State.Terminated)
            {
                Log("state terminated - stopping HealthBridge services");
                StopMainThreadHeartbeat();
                StopServer();
            }
        }

        // ────── Server lifecycle ──────

        private void StartServer()
        {
            try
            {
                _listener = new HttpListener();
                _listener.Prefixes.Add(_listenUrl);
                _listener.Start();
                _cts = new CancellationTokenSource();
                _serverTask = Task.Run(() => HandleLoop(_cts.Token));
                _workerThread = new Thread(BacktestWorkerLoop) { IsBackground = true, Name = "HealthBridge-Worker" };
                _workerThread.Start();
                Interlocked.Exchange(ref _lastRequestUtcTicks, DateTime.UtcNow.Ticks);
                Interlocked.Exchange(ref _lastMainThreadTickUtcTicks, DateTime.UtcNow.Ticks);
                Log("listening on " + _listenUrl + " (backtest worker started)");
            }
            catch (Exception ex)
            {
                Log("start failed: " + ex.Message);
            }
        }

        private void StopServer()
        {
            try
            {
                _cts?.Cancel();
                _jobQueue.CompleteAdding();
                if (_listener != null && _listener.IsListening)
                {
                    _listener.Stop();
                    _listener.Close();
                }
                Log("stopped");
            }
            catch (Exception ex)
            {
                Log("stop error: " + ex.Message);
            }
        }

        private void StartMainThreadHeartbeat()
        {
            _heartbeatRunning = true;
            _heartbeatThread = new Thread(() =>
            {
                while (_heartbeatRunning)
                {
                    try
                    {
                        string err;
                        var sw = Stopwatch.StartNew();
                        bool ok = InvokeOnMainThreadWithTimeout(() =>
                        {
                            Interlocked.Exchange(ref _lastMainThreadTickUtcTicks, DateTime.UtcNow.Ticks);
                        }, 1500, out err);
                        sw.Stop();
                        Interlocked.Exchange(ref _lastMainThreadPingMs, ok ? (int)sw.ElapsedMilliseconds : -1);
                        _lastMainThreadError = ok ? "" : err;
                    }
                    catch (Exception ex)
                    {
                        _lastMainThreadError = ex.Message;
                    }
                    Thread.Sleep(1000);
                }
            });
            _heartbeatThread.IsBackground = true;
            _heartbeatThread.Name = "HealthBridge-Heartbeat";
            _heartbeatThread.Start();
        }

        private void StopMainThreadHeartbeat()
        {
            try
            {
                _heartbeatRunning = false;
                if (_heartbeatThread != null && _heartbeatThread.IsAlive)
                    _heartbeatThread.Join(2000);
            }
            catch { }
        }

        private async Task HandleLoop(CancellationToken ct)
        {
            while (!ct.IsCancellationRequested && _listener != null && _listener.IsListening)
            {
                try
                {
                    var ctx = await _listener.GetContextAsync();
                    _ = Task.Run(() => Dispatch(ctx));
                }
                catch (HttpListenerException) { break; }
                catch (ObjectDisposedException) { break; }
                catch (Exception ex)
                {
                    if (!ct.IsCancellationRequested)
                        Log("listener error: " + ex.Message);
                }
            }
        }

        // ────── Request routing ──────

        private void Dispatch(HttpListenerContext ctx)
        {
            string body = "";
            int status = 200;
            try
            {
                Interlocked.Exchange(ref _lastRequestUtcTicks, DateTime.UtcNow.Ticks);
                string path = ctx.Request.Url.AbsolutePath.TrimEnd('/').ToLower();
                if (path == "") path = "/";
                string method = ctx.Request.HttpMethod;

                // ── Path-parametrized routes ──
                // GET /backtest/{id}  - poll job status/results
                if (method == "GET" && path.StartsWith("/backtest/"))
                {
                    string jobId = path.Substring("/backtest/".Length);
                    body = GetBacktestJobJson(jobId);
                }
                // POST /backtest  - run backtest synchronously (blocks until complete)
                else if (method == "POST" && path == "/backtest")
                {
                    body = RunBacktestSync(ctx.Request);
                }
                // POST /strategy_write  - write a .cs file to Strategies folder
                else if (method == "POST" && path == "/strategy_write")
                {
                    body = WriteStrategyFile(ctx.Request);
                }
                // POST /compile  - trigger NinjaScript compile, return diagnostics
                // Body: {"full": true} for full compile (reloads DLL), default is check-only
                else if (method == "POST" && path == "/compile")
                {
                    bool fullCompile = false;
                    try
                    {
                        string reqBody = new System.IO.StreamReader(ctx.Request.InputStream).ReadToEnd();
                        if (!string.IsNullOrEmpty(reqBody) && reqBody.Contains("\"full\""))
                            fullCompile = reqBody.Contains("\"full\":true") || reqBody.Contains("\"full\": true");
                    } catch { }
                    body = CompileNinjaScript(fullCompile);
                }
                // GET /strategy_source/{name}  - read .cs file content
                else if (method == "GET" && path.StartsWith("/strategy_source/"))
                {
                    string name = path.Substring("/strategy_source/".Length);
                    body = ReadStrategyFile(name);
                }
                // GET /backtests  - list all jobs
                else if (method == "GET" && path == "/backtests")
                {
                    body = ListBacktestJobsJson();
                }
                else if (method == "GET" && path == "/windows")
                {
                    body = ListWindowsJson();
                }
                else if (method == "GET" && path == "/sa_diag")
                {
                    body = GetSADiagJson();
                }
                else if (method == "GET" && path == "/strategies")
                {
                    body = ListStrategiesJson();
                }
                else if (method == "GET" && path == "/strategy_runtime")
                {
                    body = GetStrategyRuntimeJson();
                }
                else if (method == "GET" && path == "/healthz")
                {
                    body = GetHealthzJson();
                }
                else if (method == "GET" && path == "/runtime_snapshot")
                {
                    body = GetRuntimeSnapshotJson();
                }
                else if (method == "POST" && path == "/recover/reconnect")
                {
                    body = RecoverReconnectJson(false, ctx.Request);
                }
                else if (method == "POST" && path == "/recover/flatten_then_reconnect")
                {
                    body = RecoverReconnectJson(true, ctx.Request);
                }
                else
                {
                    switch (path)
                    {
                        case "/":
                        case "/health":
                            body = "{\"status\":\"ok\",\"service\":\"HealthBridge\",\"version\":\"0.4.0\",\"build_id\":\"" + BuildId + "\"}";
                            break;
                        case "/accounts":
                            body = GetAccountsJson();
                            break;
                        case "/positions":
                            body = GetPositionsJson();
                            break;
                        case "/connections":
                            body = GetConnectionsJson();
                            break;
                        case "/orders":
                            body = GetOrdersJson();
                            break;
                        case "/trades":
                            body = GetTradesJson(ctx.Request.QueryString);
                            break;
                        case "/daily_pnl":
                            body = GetDailyPnlJson(ctx.Request.QueryString);
                            break;
                        default:
                            status = 404;
                            body = "{\"error\":\"unknown endpoint\",\"path\":\"" + JsonEscape(path) + "\",\"method\":\"" + method + "\"}";
                            break;
                    }
                }
            }
            catch (Exception ex)
            {
                status = 500;
                body = "{\"error\":\"" + JsonEscape(ex.Message) + "\"}";
                Log("dispatch error: " + ex.Message);
            }

            try
            {
                var bytes = Encoding.UTF8.GetBytes(body);
                ctx.Response.StatusCode = status;
                ctx.Response.ContentType = "application/json";
                ctx.Response.ContentLength64 = bytes.Length;
                ctx.Response.Headers.Add("Access-Control-Allow-Origin", "*");
                ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
                ctx.Response.Close();
            }
            catch { /* client disconnected */ }
        }

        // ────── Endpoint handlers ──────

        private string GetAccountsJson()
        {
            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var acc in Account.All)
            {
                if (!first) sb.Append(",");
                sb.Append("{");
                sb.Append("\"name\":\"").Append(JsonEscape(acc.Name)).Append("\",");
                sb.Append("\"connected\":").Append(acc.Connection != null ? "true" : "false").Append(",");
                try
                {
                    sb.Append("\"cash\":").Append(acc.Get(AccountItem.CashValue, Currency.UsDollar).ToString("F2")).Append(",");
                    sb.Append("\"realized_pnl\":").Append(acc.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar).ToString("F2")).Append(",");
                    sb.Append("\"unrealized_pnl\":").Append(acc.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar).ToString("F2")).Append(",");
                    sb.Append("\"buying_power\":").Append(acc.Get(AccountItem.BuyingPower, Currency.UsDollar).ToString("F2"));
                }
                catch
                {
                    sb.Append("\"cash\":null,\"realized_pnl\":null,\"unrealized_pnl\":null,\"buying_power\":null");
                }
                sb.Append("}");
                first = false;
            }
            sb.Append("]");
            return sb.ToString();
        }

        private string GetPositionsJson()
        {
            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var acc in Account.All)
            {
                foreach (var pos in acc.Positions)
                {
                    if (pos.MarketPosition == MarketPosition.Flat) continue;
                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"account\":\"").Append(JsonEscape(acc.Name)).Append("\",");
                    sb.Append("\"instrument\":\"").Append(JsonEscape(pos.Instrument.FullName)).Append("\",");
                    sb.Append("\"side\":\"").Append(pos.MarketPosition).Append("\",");
                    sb.Append("\"quantity\":").Append(pos.Quantity).Append(",");
                    sb.Append("\"avg_price\":").Append(pos.AveragePrice.ToString("F4")).Append(",");
                    sb.Append("\"unrealized\":").Append(pos.GetUnrealizedProfitLoss(PerformanceUnit.Currency).ToString("F2"));
                    sb.Append("}");
                    first = false;
                }
            }
            sb.Append("]");
            return sb.ToString();
        }

        private string GetConnectionsJson()
        {
            var sb = new StringBuilder("[");
            bool first = true;
            var seenNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            string err;
            bool ok = InvokeOnMainThreadWithTimeout(() =>
            {
                foreach (var conn in Connection.Connections)
                {
                    string name = "";
                    string status = "Unknown";
                    try { name = conn.Options != null ? conn.Options.Name : ""; } catch { }
                    try { status = conn.Status.ToString(); } catch { }
                    if (string.IsNullOrEmpty(name))
                        name = "UnnamedConnection";
                    seenNames.Add(name);

                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"name\":\"").Append(JsonEscape(name)).Append("\",");
                    sb.Append("\"status\":\"").Append(JsonEscape(status)).Append("\",");
                    sb.Append("\"source\":\"runtime\"");
                    sb.Append("}");
                    first = false;
                }

                foreach (var option in GetConfiguredConnectionOptions())
                {
                    string name = GetAnyStringProperty(option, new[] { "Name", "DisplayName", "ConnectionName" });
                    if (string.IsNullOrEmpty(name))
                        continue;
                    if (seenNames.Contains(name))
                        continue;

                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"name\":\"").Append(JsonEscape(name)).Append("\",");
                    sb.Append("\"status\":\"Configured\",");
                    sb.Append("\"source\":\"configured\"");
                    sb.Append("}");
                    first = false;
                }

                foreach (var name in GetConfiguredConnectionNamesFromConfig())
                {
                    if (string.IsNullOrEmpty(name))
                        continue;
                    if (seenNames.Contains(name))
                        continue;
                    seenNames.Add(name);

                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"name\":\"").Append(JsonEscape(name)).Append("\",");
                    sb.Append("\"status\":\"Configured\",");
                    sb.Append("\"source\":\"config_xml\"");
                    sb.Append("}");
                    first = false;
                }
            }, 3000, out err);

            if (!ok)
            {
                if (!first) sb.Append(",");
                sb.Append("{\"name\":\"error\",\"status\":\"").Append(JsonEscape(err)).Append("\",\"source\":\"bridge\"}");
            }

            sb.Append("]");
            return sb.ToString();
        }

        private string GetOrdersJson()
        {
            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var acc in Account.All)
            {
                foreach (var ord in acc.Orders)
                {
                    // Only include active orders
                    if (ord.OrderState != OrderState.Working
                        && ord.OrderState != OrderState.Accepted
                        && ord.OrderState != OrderState.Submitted
                        && ord.OrderState != OrderState.PartFilled)
                        continue;
                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"account\":\"").Append(JsonEscape(acc.Name)).Append("\",");
                    sb.Append("\"instrument\":\"").Append(JsonEscape(ord.Instrument.FullName)).Append("\",");
                    sb.Append("\"action\":\"").Append(ord.OrderAction).Append("\",");
                    sb.Append("\"type\":\"").Append(ord.OrderType).Append("\",");
                    sb.Append("\"state\":\"").Append(ord.OrderState).Append("\",");
                    sb.Append("\"quantity\":").Append(ord.Quantity).Append(",");
                    sb.Append("\"limit_price\":").Append(ord.LimitPrice.ToString("F4")).Append(",");
                    sb.Append("\"stop_price\":").Append(ord.StopPrice.ToString("F4")).Append(",");
                    sb.Append("\"time\":\"").Append(ord.Time.ToString("yyyy-MM-ddTHH:mm:ss")).Append("\"");
                    sb.Append("}");
                    first = false;
                }
            }
            sb.Append("]");
            return sb.ToString();
        }

        private string GetTradesJson(System.Collections.Specialized.NameValueCollection qs)
        {
            string filterAccount = qs["account"];
            int days = 7;
            int.TryParse(qs["days"], out days);
            if (days <= 0) days = 7;
            DateTime cutoff = DateTime.Now.AddDays(-days);

            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var acc in Account.All)
            {
                if (!string.IsNullOrEmpty(filterAccount)
                    && !acc.Name.Equals(filterAccount, StringComparison.OrdinalIgnoreCase))
                    continue;

                SystemPerformance perf;
                try { perf = SystemPerformance.Calculate(acc.Executions); }
                catch { continue; }

                foreach (var trade in perf.AllTrades)
                {
                    if (trade.Exit == null) continue;
                    if (trade.Exit.Time < cutoff) continue;
                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"account\":\"").Append(JsonEscape(acc.Name)).Append("\",");
                    sb.Append("\"instrument\":\"").Append(JsonEscape(trade.Entry.Instrument.FullName)).Append("\",");
                    sb.Append("\"entry_time\":\"").Append(trade.Entry.Time.ToString("yyyy-MM-ddTHH:mm:ss")).Append("\",");
                    sb.Append("\"entry_price\":").Append(trade.Entry.Price.ToString("F4")).Append(",");
                    sb.Append("\"exit_time\":\"").Append(trade.Exit.Time.ToString("yyyy-MM-ddTHH:mm:ss")).Append("\",");
                    sb.Append("\"exit_price\":").Append(trade.Exit.Price.ToString("F4")).Append(",");
                    sb.Append("\"quantity\":").Append(trade.Quantity).Append(",");
                    sb.Append("\"profit_currency\":").Append(trade.ProfitCurrency.ToString("F2")).Append(",");
                    sb.Append("\"profit_points\":").Append(trade.ProfitPoints.ToString("F4")).Append(",");
                    sb.Append("\"side\":\"").Append(trade.Entry.MarketPosition).Append("\"");
                    sb.Append("}");
                    first = false;
                }
            }
            sb.Append("]");
            return sb.ToString();
        }

        private string GetDailyPnlJson(System.Collections.Specialized.NameValueCollection qs)
        {
            string filterAccount = qs["account"];
            DateTime today = DateTime.Today;

            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var acc in Account.All)
            {
                if (!string.IsNullOrEmpty(filterAccount)
                    && !acc.Name.Equals(filterAccount, StringComparison.OrdinalIgnoreCase))
                    continue;

                double realized = 0;
                int tradeCount = 0;
                int wins = 0;
                int losses = 0;

                SystemPerformance perf2;
                try { perf2 = SystemPerformance.Calculate(acc.Executions); }
                catch { perf2 = null; }

                if (perf2 != null)
                foreach (var trade in perf2.AllTrades)
                {
                    if (trade.Exit == null) continue;
                    if (trade.Exit.Time.Date != today) continue;
                    realized += trade.ProfitCurrency;
                    tradeCount++;
                    if (trade.ProfitCurrency > 0) wins++;
                    else if (trade.ProfitCurrency < 0) losses++;
                }

                double unrealized = 0;
                try { unrealized = acc.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar); }
                catch { }

                if (!first) sb.Append(",");
                sb.Append("{");
                sb.Append("\"account\":\"").Append(JsonEscape(acc.Name)).Append("\",");
                sb.Append("\"date\":\"").Append(today.ToString("yyyy-MM-dd")).Append("\",");
                sb.Append("\"realized_pnl\":").Append(realized.ToString("F2")).Append(",");
                sb.Append("\"unrealized_pnl\":").Append(unrealized.ToString("F2")).Append(",");
                sb.Append("\"total_pnl\":").Append((realized + unrealized).ToString("F2")).Append(",");
                sb.Append("\"trades\":").Append(tradeCount).Append(",");
                sb.Append("\"wins\":").Append(wins).Append(",");
                sb.Append("\"losses\":").Append(losses);
                sb.Append("}");
                first = false;
            }
            sb.Append("]");
            return sb.ToString();
        }

        // ────── Diagnostics ──────

        private string ListWindowsJson()
        {
            var sb = new StringBuilder("[");
            bool first = true;
            var disp = NinjaTrader.Core.Globals.MainThreadDispatcher;
            disp.Invoke(new Action(() =>
            {
                foreach (var w in NinjaTrader.Core.Globals.AllWindows)
                {
                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"type\":\"").Append(JsonEscape(w.GetType().FullName)).Append("\",");
                    try { sb.Append("\"title\":\"").Append(JsonEscape(w.Title ?? "")).Append("\""); }
                    catch { sb.Append("\"title\":null"); }
                    sb.Append("}");
                    first = false;
                }
            }));
            sb.Append("]");
            return sb.ToString();
        }

        private string ListStrategiesJson()
        {
            // Walk all loaded assemblies, find types in NinjaTrader.NinjaScript.Strategies that derive from Strategy
            var sb = new StringBuilder("[");
            bool first = true;
            var strategyBase = typeof(NinjaTrader.NinjaScript.Strategies.Strategy);
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = asm.GetTypes(); }
                catch (System.Reflection.ReflectionTypeLoadException ex) { types = ex.Types; }
                catch { continue; }
                foreach (var t in types)
                {
                    if (t == null) continue;
                    if (!t.IsClass || t.IsAbstract || !t.IsPublic) continue;
                    if (t.Namespace != "NinjaTrader.NinjaScript.Strategies") continue;
                    if (!strategyBase.IsAssignableFrom(t)) continue;
                    if (!first) sb.Append(",");
                    sb.Append("{\"name\":\"").Append(JsonEscape(t.Name)).Append("\",\"assembly\":\"").Append(JsonEscape(asm.GetName().Name)).Append("\"}");
                    first = false;
                }
            }
            sb.Append("]");
            return sb.ToString();
        }

        private string GetHealthzJson()
        {
            var nowUtc = DateTime.UtcNow;
            var lastRequestUtc = new DateTime(Interlocked.Read(ref _lastRequestUtcTicks), DateTimeKind.Utc);
            var lastMainThreadTickUtc = new DateTime(Interlocked.Read(ref _lastMainThreadTickUtcTicks), DateTimeKind.Utc);
            var requestAgeSec = (nowUtc - lastRequestUtc).TotalSeconds;
            var mainThreadAgeSec = (nowUtc - lastMainThreadTickUtc).TotalSeconds;
            var uptimeSec = (nowUtc - new DateTime(_startedUtcTicks, DateTimeKind.Utc)).TotalSeconds;

            int totalConnections = 0;
            int connectedConnections = 0;
            int unstableConnections = 0;
            int configuredConnections = 0;
            string connectionErr;
            bool connectionsOk = InvokeOnMainThreadWithTimeout(() =>
            {
                foreach (var conn in Connection.Connections)
                {
                    totalConnections++;
                    var status = conn.Status.ToString();
                    if (status.Equals("Connected", StringComparison.OrdinalIgnoreCase))
                        connectedConnections++;
                    else if (status.IndexOf("Lost", StringComparison.OrdinalIgnoreCase) >= 0
                        || status.IndexOf("Reconnect", StringComparison.OrdinalIgnoreCase) >= 0
                        || status.IndexOf("Disconnected", StringComparison.OrdinalIgnoreCase) >= 0)
                        unstableConnections++;
                }
                var configuredNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (var option in GetConfiguredConnectionOptions())
                {
                    string optionName = GetAnyStringProperty(option, new[] { "Name", "DisplayName", "ConnectionName" });
                    if (!string.IsNullOrEmpty(optionName))
                        configuredNames.Add(optionName);
                }
                foreach (var name in GetConfiguredConnectionNamesFromConfig())
                {
                    if (!string.IsNullOrEmpty(name))
                        configuredNames.Add(name);
                }
                configuredConnections = configuredNames.Count;
            }, 3000, out connectionErr);

            int blockingCount;
            string blockingWindowsJson = GetBlockingWindowsJson(out blockingCount);

            var reasons = new List<string>();
            string statusLevel = "ok";
            if (mainThreadAgeSec > 10)
            {
                statusLevel = "stuck";
                reasons.Add("main_thread_unresponsive");
            }
            if (totalConnections == 0)
            {
                if (statusLevel != "stuck") statusLevel = "degraded";
                if (configuredConnections > 0)
                    reasons.Add("all_connections_down");
                else
                    reasons.Add("no_connections_detected");
            }
            else if (connectedConnections == 0)
            {
                if (statusLevel != "stuck") statusLevel = "degraded";
                reasons.Add("all_connections_down");
            }
            else if (unstableConnections > 0)
            {
                if (statusLevel != "stuck") statusLevel = "degraded";
                reasons.Add("connection_unstable");
            }
            if (blockingCount > 0)
            {
                if (statusLevel != "stuck") statusLevel = "degraded";
                reasons.Add("blocking_window_detected");
            }
            if (!string.IsNullOrEmpty(_lastMainThreadError))
            {
                if (statusLevel != "stuck") statusLevel = "degraded";
                reasons.Add("main_thread_ping_error");
            }
            if (!connectionsOk)
            {
                if (statusLevel != "stuck") statusLevel = "degraded";
                reasons.Add("connection_query_error");
            }

            var sb = new StringBuilder("{");
            sb.Append("\"status\":\"").Append(statusLevel).Append("\",");
            sb.Append("\"service\":\"HealthBridge\",");
            sb.Append("\"version\":\"0.4.0\",");
            sb.Append("\"build_id\":\"").Append(BuildId).Append("\",");
            sb.Append("\"now_utc\":\"").Append(nowUtc.ToString("yyyy-MM-ddTHH:mm:ssZ")).Append("\",");
            sb.Append("\"uptime_sec\":").Append(uptimeSec.ToString("F0")).Append(",");
            sb.Append("\"request_age_sec\":").Append(requestAgeSec.ToString("F1")).Append(",");
            sb.Append("\"mainthread_age_sec\":").Append(mainThreadAgeSec.ToString("F1")).Append(",");
            sb.Append("\"mainthread_ping_ms\":").Append(Interlocked.CompareExchange(ref _lastMainThreadPingMs, 0, 0)).Append(",");
            sb.Append("\"mainthread_error\":\"").Append(JsonEscape(_lastMainThreadError ?? "")).Append("\",");
            sb.Append("\"connections\":{\"total\":").Append(totalConnections)
                .Append(",\"connected\":").Append(connectedConnections)
                .Append(",\"unstable\":").Append(unstableConnections)
                .Append(",\"configured\":").Append(configuredConnections)
                .Append("},");
            sb.Append("\"blocking_windows_count\":").Append(blockingCount).Append(",");
            sb.Append("\"blocking_windows\":").Append(blockingWindowsJson).Append(",");
            sb.Append("\"reasons\":[");
            for (int i = 0; i < reasons.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append("\"").Append(JsonEscape(reasons[i])).Append("\"");
            }
            sb.Append("],");
            sb.Append("\"last_recovery\":{");
            var lastRecoveryTicks = Interlocked.Read(ref _lastRecoveryAttemptUtcTicks);
            sb.Append("\"attempt_utc\":\"").Append(lastRecoveryTicks > 0 ? new DateTime(lastRecoveryTicks, DateTimeKind.Utc).ToString("yyyy-MM-ddTHH:mm:ssZ") : "").Append("\",");
            sb.Append("\"action\":\"").Append(JsonEscape(_lastRecoveryAction)).Append("\",");
            sb.Append("\"result\":\"").Append(JsonEscape(_lastRecoveryResult)).Append("\",");
            sb.Append("\"error\":\"").Append(JsonEscape(_lastRecoveryError)).Append("\"");
            sb.Append("}");
            sb.Append("}");
            return sb.ToString();
        }

        private string GetBlockingWindowsJson(out int blockingCount)
        {
            blockingCount = 0;
            int localBlockingCount = 0;
            var knownBlockingTokens = new[]
            {
                "error", "warning", "disconnected", "connection lost", "login", "log in",
                "confirm", "reconnect", "exception", "license", "restart"
            };
            var sb = new StringBuilder("[");
            bool first = true;
            string err;
            bool ok = InvokeOnMainThreadWithTimeout(() =>
            {
                foreach (var w in NinjaTrader.Core.Globals.AllWindows)
                {
                    var typeName = w.GetType().FullName ?? "";
                    string title = "";
                    try { title = w.Title ?? ""; } catch { }
                    string lower = (typeName + " " + title).ToLowerInvariant();
                    bool isBlocking = typeName.IndexOf("Dialog", StringComparison.OrdinalIgnoreCase) >= 0
                        || typeName.IndexOf("MessageBox", StringComparison.OrdinalIgnoreCase) >= 0;
                    if (!isBlocking)
                    {
                        foreach (var token in knownBlockingTokens)
                        {
                            if (lower.IndexOf(token, StringComparison.OrdinalIgnoreCase) >= 0)
                            {
                                isBlocking = true;
                                break;
                            }
                        }
                    }
                    if (!isBlocking)
                        continue;

                    localBlockingCount++;
                    if (!first) sb.Append(",");
                    sb.Append("{");
                    sb.Append("\"type\":\"").Append(JsonEscape(typeName)).Append("\",");
                    sb.Append("\"title\":\"").Append(JsonEscape(title)).Append("\",");
                    sb.Append("\"is_blocking\":true");
                    sb.Append("}");
                    first = false;
                }
            }, 2000, out err);
            if (!ok)
            {
                blockingCount = 1;
                return "[{\"type\":\"error\",\"title\":\"" + JsonEscape(err) + "\",\"is_blocking\":true}]";
            }
            blockingCount = localBlockingCount;
            sb.Append("]");
            return sb.ToString();
        }

        private string GetStrategyRuntimeJson()
        {
            var sb = new StringBuilder("{");
            int total = 0;
            int active = 0;
            bool foundCollection = false;
            string strategyItemsJson = "[]";
            string err;
            bool ok = InvokeOnMainThreadWithTimeout(() =>
            {
                object controlCenter = null;
                foreach (var w in NinjaTrader.Core.Globals.AllWindows)
                {
                    var fullName = w.GetType().FullName ?? "";
                    if (fullName.IndexOf("ControlCenter", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        controlCenter = w;
                        break;
                    }
                }
                if (controlCenter == null)
                    return;

                object source = null;
                object vm;
                if (TryGetPropertyValue(controlCenter, "ViewModel", out vm) && vm != null)
                {
                    if (!TryGetPropertyValue(vm, "Strategies", out source) || source == null)
                        TryGetPropertyValue(vm, "StrategyRows", out source);
                }
                if (source == null)
                {
                    if (!TryGetPropertyValue(controlCenter, "Strategies", out source) || source == null)
                        TryGetPropertyValue(controlCenter, "StrategyRows", out source);
                }
                if (source == null)
                    return;

                var enumerable = source as System.Collections.IEnumerable;
                if (enumerable == null)
                    return;
                foundCollection = true;

                var arr = new StringBuilder("[");
                bool first = true;
                foreach (var row in enumerable)
                {
                    if (row == null) continue;
                    total++;
                    bool isEnabled = GetAnyBooleanProperty(row, new[] { "IsEnabled", "Enabled", "IsActive", "IsRunning" });
                    if (isEnabled) active++;
                    if (!first) arr.Append(",");
                    arr.Append("{");
                    arr.Append("\"name\":\"").Append(JsonEscape(GetAnyStringProperty(row, new[] { "Name", "DisplayName", "Strategy", "StrategyName" }))).Append("\",");
                    arr.Append("\"account\":\"").Append(JsonEscape(GetAnyStringProperty(row, new[] { "AccountName", "Account", "AccountDisplayName" }))).Append("\",");
                    arr.Append("\"instrument\":\"").Append(JsonEscape(GetAnyStringProperty(row, new[] { "Instrument", "InstrumentName", "InstrumentDisplayName" }))).Append("\",");
                    arr.Append("\"template\":\"").Append(JsonEscape(GetAnyStringProperty(row, new[] { "Template", "TemplateName", "AtmStrategyTemplate" }))).Append("\",");
                    arr.Append("\"state\":\"").Append(JsonEscape(GetAnyStringProperty(row, new[] { "State", "Status" }))).Append("\",");
                    arr.Append("\"is_enabled\":").Append(isEnabled ? "true" : "false");
                    arr.Append("}");
                    first = false;
                }
                arr.Append("]");
                strategyItemsJson = arr.ToString();
            }, 3000, out err);

            sb.Append("\"collection_found\":").Append(foundCollection ? "true" : "false").Append(",");
            sb.Append("\"active_count\":").Append(active).Append(",");
            sb.Append("\"total_count\":").Append(total).Append(",");
            sb.Append("\"strategies\":").Append(strategyItemsJson).Append(",");
            sb.Append("\"error\":\"").Append(ok ? "" : JsonEscape(err)).Append("\"");
            sb.Append("}");
            return sb.ToString();
        }

        private string GetRuntimeSnapshotJson()
        {
            var sb = new StringBuilder("{");
            sb.Append("\"generated_utc\":\"").Append(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")).Append("\",");
            sb.Append("\"health\":").Append(GetHealthzJson()).Append(",");
            sb.Append("\"connections\":").Append(GetConnectionsJson()).Append(",");
            sb.Append("\"strategy_runtime\":").Append(GetStrategyRuntimeJson()).Append(",");
            int blockingCount;
            sb.Append("\"blocking_windows\":").Append(GetBlockingWindowsJson(out blockingCount)).Append(",");
            sb.Append("\"blocking_windows_count\":").Append(blockingCount).Append(",");
            sb.Append("\"accounts\":").Append(GetAccountsJson()).Append(",");
            sb.Append("\"positions\":").Append(GetPositionsJson());
            sb.Append("}");
            return sb.ToString();
        }

        private string RecoverReconnectJson(bool flattenFirst, HttpListenerRequest request)
        {
            var startedUtc = DateTime.UtcNow;
            Interlocked.Exchange(ref _lastRecoveryAttemptUtcTicks, startedUtc.Ticks);
            _lastRecoveryAction = flattenFirst ? "flatten_then_reconnect" : "reconnect";
            _lastRecoveryResult = "running";
            _lastRecoveryError = "";
            var targetConnectionNames = ParseConnectionNames(request);
            var targetNameSet = new HashSet<string>(targetConnectionNames, StringComparer.OrdinalIgnoreCase);

            int flattenAttempts = 0;
            int flattenSucceeded = 0;
            int flattenFailed = 0;
            int reconnectAttempts = 0;
            int reconnectSucceeded = 0;
            int reconnectFailed = 0;
            int totalConnectionsSeen = 0;
            string opError = "";
            int postTotalConnections = -1;
            int postConnectedConnections = -1;
            var debugTrail = new List<string>();

            string dispatcherErr;
            bool ok = InvokeOnMainThreadWithTimeout(() =>
            {
                if (flattenFirst)
                {
                    foreach (var acc in Account.All)
                    {
                        flattenAttempts++;
                        string err;
                        bool invoked = InvokeBestEffortNoArg(acc, new[] { "Flatten", "FlattenEverything" }, out err);
                        if (invoked) flattenSucceeded++;
                        else
                        {
                            flattenFailed++;
                            if (!string.IsNullOrEmpty(err))
                                opError = opError + (string.IsNullOrEmpty(opError) ? "" : " | ") + acc.Name + ": " + err;
                        }
                    }
                }

                foreach (var conn in Connection.Connections)
                {
                    string connName = "";
                    try { connName = conn.Options != null ? conn.Options.Name : ""; } catch { }
                    if (!ConnectionNameMatchesFilter(connName, targetNameSet))
                        continue;
                    totalConnectionsSeen++;
                    var status = conn.Status.ToString();
                    if (status.Equals("Connected", StringComparison.OrdinalIgnoreCase))
                        continue;
                    reconnectAttempts++;
                    string err;
                    bool invoked = TryInvokeConnectMethodsOnTarget(conn, connName, null, out err);
                    if (invoked) reconnectSucceeded++;
                    else
                    {
                        reconnectFailed++;
                        if (!string.IsNullOrEmpty(err))
                            opError = opError + (string.IsNullOrEmpty(opError) ? "" : " | ")
                                + (string.IsNullOrEmpty(connName) ? "runtime_connection" : connName) + ": " + err;
                    }
                }

                // Fallback: when no runtime connection instances exist, try configured options.
                if (totalConnectionsSeen == 0)
                {
                    var attemptedConfiguredNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    foreach (var option in GetConfiguredConnectionOptions())
                    {
                        string optionName = GetAnyStringProperty(option, new[] { "Name", "DisplayName", "ConnectionName" });
                        if (!ConnectionNameMatchesFilter(optionName, targetNameSet))
                            continue;
                        totalConnectionsSeen++;
                        reconnectAttempts++;
                        if (!string.IsNullOrEmpty(optionName))
                            attemptedConfiguredNames.Add(optionName);
                        string err;
                        bool invoked = TryConnectConfiguredOption(option, out err);
                        if (invoked) reconnectSucceeded++;
                        else
                        {
                            reconnectFailed++;
                            if (!string.IsNullOrEmpty(err))
                                opError = opError + (string.IsNullOrEmpty(opError) ? "" : " | ")
                                    + (string.IsNullOrEmpty(optionName) ? "configured_connection" : optionName)
                                    + ": " + err;
                        }
                    }

                    bool triedNoArgConnect = false;
                    var configuredNames = targetConnectionNames.Count > 0
                        ? targetConnectionNames
                        : GetConfiguredConnectionNamesFromConfig();
                    foreach (var configuredName in configuredNames)
                    {
                        if (string.IsNullOrEmpty(configuredName))
                            continue;
                        if (attemptedConfiguredNames.Contains(configuredName))
                            continue;
                        totalConnectionsSeen++;
                        reconnectAttempts++;
                        string err;
                        string pathTag;
                        bool invoked = TryConnectConfiguredName(configuredName, out err, out pathTag);
                        if (invoked)
                            debugTrail.Add(configuredName + "=>" + pathTag);
                        if (!invoked && !triedNoArgConnect && targetConnectionNames.Count == 0)
                        {
                            string fallbackErr;
                            bool fallbackInvoked = TryConnectAnyConfiguredNoArg(out fallbackErr);
                            triedNoArgConnect = true;
                            if (fallbackInvoked)
                            {
                                invoked = true;
                                err = "";
                                debugTrail.Add(configuredName + "=>noarg_static");
                            }
                            else if (!string.IsNullOrEmpty(fallbackErr))
                            {
                                err = string.IsNullOrEmpty(err) ? fallbackErr : (err + " | " + fallbackErr);
                            }
                        }
                        if (invoked) reconnectSucceeded++;
                        else
                        {
                            reconnectFailed++;
                            if (!string.IsNullOrEmpty(err))
                                opError = opError + (string.IsNullOrEmpty(opError) ? "" : " | ") + configuredName + ": " + err;
                        }
                    }
                }
            }, 10000, out dispatcherErr);
            if (!ok)
                opError = string.IsNullOrEmpty(opError) ? dispatcherErr : opError + " | " + dispatcherErr;
            if (totalConnectionsSeen == 0)
                opError = string.IsNullOrEmpty(opError) ? "no connections available for reconnect" : opError + " | no connections available for reconnect";

            string verifyErr = "";
            bool verifyOk = false;
            for (int i = 0; i < 4; i++)
            {
                Thread.Sleep(750);
                int localTotal = 0;
                int localConnected = 0;
                string stepErr;
                bool stepOk = InvokeOnMainThreadWithTimeout(() =>
                {
                    foreach (var conn in Connection.Connections)
                    {
                        localTotal++;
                        var status = conn.Status.ToString();
                        if (status.Equals("Connected", StringComparison.OrdinalIgnoreCase))
                            localConnected++;
                    }
                }, 2000, out stepErr);
                if (stepOk)
                {
                    verifyOk = true;
                    postTotalConnections = localTotal;
                    postConnectedConnections = localConnected;
                    if (postConnectedConnections > 0)
                        break;
                }
                else
                {
                    verifyErr = stepErr;
                }
            }
            if (!verifyOk && !string.IsNullOrEmpty(verifyErr))
                opError = string.IsNullOrEmpty(opError) ? verifyErr : opError + " | " + verifyErr;
            if (verifyOk && postConnectedConnections <= 0)
                opError = string.IsNullOrEmpty(opError)
                    ? "reconnect_invoked_but_no_connected_runtime_connection"
                    : opError + " | reconnect_invoked_but_no_connected_runtime_connection";

            bool success = ok
                && totalConnectionsSeen > 0
                && reconnectFailed == 0
                && (!flattenFirst || flattenFailed == 0)
                && verifyOk
                && postConnectedConnections > 0;
            _lastRecoveryResult = success ? "success" : "failed";
            _lastRecoveryError = opError;
            Log("recovery action " + (flattenFirst ? "flatten_then_reconnect" : "reconnect")
                + " => " + (success ? "success" : "failed")
                + " | total connections=" + totalConnectionsSeen
                + " | reconnect attempted=" + reconnectAttempts + " failed=" + reconnectFailed
                + " | post connections=" + postConnectedConnections + "/" + postTotalConnections
                + " | flatten attempted=" + flattenAttempts + " failed=" + flattenFailed
                + " | targets=" + (targetConnectionNames.Count > 0 ? string.Join(";", targetConnectionNames) : "all")
                + (string.IsNullOrEmpty(opError) ? "" : " | error=" + opError));

            var elapsedMs = (DateTime.UtcNow - startedUtc).TotalMilliseconds;
            var sb = new StringBuilder("{");
            sb.Append("\"success\":").Append(success ? "true" : "false").Append(",");
            sb.Append("\"action\":\"").Append(flattenFirst ? "flatten_then_reconnect" : "reconnect").Append("\",");
            sb.Append("\"started_utc\":\"").Append(startedUtc.ToString("yyyy-MM-ddTHH:mm:ssZ")).Append("\",");
            sb.Append("\"elapsed_ms\":").Append(elapsedMs.ToString("F0")).Append(",");
            sb.Append("\"target_connection_names\":[");
            for (int i = 0; i < targetConnectionNames.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append("\"").Append(JsonEscape(targetConnectionNames[i])).Append("\"");
            }
            sb.Append("],");
            sb.Append("\"post_check\":{\"total\":").Append(postTotalConnections).Append(",\"connected\":").Append(postConnectedConnections).Append("},");
            sb.Append("\"flatten\":{\"attempted\":").Append(flattenAttempts).Append(",\"succeeded\":").Append(flattenSucceeded).Append(",\"failed\":").Append(flattenFailed).Append("},");
            sb.Append("\"reconnect\":{\"attempted\":").Append(reconnectAttempts).Append(",\"succeeded\":").Append(reconnectSucceeded).Append(",\"failed\":").Append(reconnectFailed).Append("},");
            sb.Append("\"debug_trail\":[");
            for (int i = 0; i < debugTrail.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append("\"").Append(JsonEscape(debugTrail[i])).Append("\"");
            }
            sb.Append("],");
            sb.Append("\"error\":\"").Append(JsonEscape(opError)).Append("\"");
            sb.Append("}");
            return sb.ToString();
        }

        private string GetSADiagJson()
        {
            var sb = new StringBuilder("{");
            try
            {
                var lookupDisp = NinjaTrader.Core.Globals.MainThreadDispatcher;
                object saWindow = null;
                lookupDisp.Invoke(new Action(() => {
                    foreach (var w in NinjaTrader.Core.Globals.AllWindows)
                        if (w.GetType().FullName == "NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzer")
                        { saWindow = w; return; }
                }));
                if (saWindow == null) return "{\"error\":\"SA not open\"}";

                var disp = ((System.Windows.Threading.DispatcherObject)saWindow).Dispatcher;
                disp.Invoke(new Action(() => {
                    try {
                        var vm = saWindow.GetType().GetProperty("ViewModel").GetValue(saWindow, null);
                        var tab = vm.GetType().GetProperty("SelectedTab").GetValue(vm, null);
                        var props = tab.GetType().GetProperty("TabStrategyProperties").GetValue(tab, null);
                        var template = props.GetType().GetProperty("StrategyTemplate").GetValue(props, null);

                        sb.Append("\"tab_NeedsStrategyRerun\":").Append(tab.GetType().GetProperty("NeedsStrategyRerun").GetValue(tab, null)).Append(",");
                        sb.Append("\"tab_IsProgressVisible\":").Append(tab.GetType().GetProperty("IsProgressVisible").GetValue(tab, null)).Append(",");
                        sb.Append("\"tab_IsRunVisible\":").Append(tab.GetType().GetProperty("IsRunVisible").GetValue(tab, null)).Append(",");
                        sb.Append("\"tab_IsRunningDetailsRun\":").Append(tab.GetType().GetProperty("IsRunningDetailsRun").GetValue(tab, null)).Append(",");
                        var results = tab.GetType().GetProperty("Results").GetValue(tab, null);
                        sb.Append("\"tab_Results_Count\":").Append(results.GetType().GetProperty("Count").GetValue(results, null)).Append(",");
                        var selRes = tab.GetType().GetProperty("SelectedResult").GetValue(tab, null);
                        sb.Append("\"tab_SelectedResult_null\":").Append(selRes == null ? "true" : "false").Append(",");
                        sb.Append("\"props_Strategy\":\"").Append(JsonEscape((string)props.GetType().GetProperty("Strategy").GetValue(props, null) ?? "")).Append("\",");
                        sb.Append("\"props_InstrumentOrInstrumentList\":\"").Append(JsonEscape((string)props.GetType().GetProperty("InstrumentOrInstrumentList").GetValue(props, null) ?? "")).Append("\",");
                        sb.Append("\"props_SelectedRunGuid\":\"").Append(JsonEscape((string)props.GetType().GetProperty("SelectedRunGuid").GetValue(props, null) ?? "")).Append("\",");
                        if (template != null) {
                            sb.Append("\"template_State\":\"").Append(template.GetType().GetProperty("State").GetValue(template, null)).Append("\",");
                            var sp = template.GetType().GetProperty("SystemPerformance").GetValue(template, null);
                            if (sp != null) {
                                var at = sp.GetType().GetProperty("AllTrades").GetValue(sp, null);
                                int tcount = (int)at.GetType().GetProperty("Count").GetValue(at, null);
                                sb.Append("\"template_TradeCount\":").Append(tcount).Append(",");
                            } else sb.Append("\"template_SystemPerformance_null\":true,");
                            sb.Append("\"template_From\":\"").Append(((DateTime)template.GetType().GetProperty("From").GetValue(template, null)).ToString("yyyy-MM-dd")).Append("\",");
                            sb.Append("\"template_To\":\"").Append(((DateTime)template.GetType().GetProperty("To").GetValue(template, null)).ToString("yyyy-MM-dd")).Append("\",");
                            var tbp = template.GetType().GetProperty("BarsPeriod").GetValue(template, null);
                            if (tbp != null) {
                                sb.Append("\"template_BarsPeriodType\":\"").Append(tbp.GetType().GetProperty("BarsPeriodType").GetValue(tbp, null)).Append("\",");
                                sb.Append("\"template_BarsPeriodValue\":").Append(tbp.GetType().GetProperty("Value").GetValue(tbp, null));
                            } else sb.Append("\"template_BarsPeriod\":null");
                        }
                    } catch (Exception ex) { sb.Append("\"error\":\"").Append(JsonEscape(ex.Message)).Append("\""); }
                }));
            } catch (Exception ex) { sb.Append("\"error\":\"").Append(JsonEscape(ex.Message)).Append("\""); }
            sb.Append("}");
            return sb.ToString();
        }

        // ────── Strategy development endpoints (write, compile, read source) ──────

        private class WriteFileRequest { public string name { get; set; } public string code { get; set; } public string folder { get; set; } }

        private string WriteStrategyFile(HttpListenerRequest req)
        {
            string bodyText;
            using (var reader = new StreamReader(req.InputStream, req.ContentEncoding))
                bodyText = reader.ReadToEnd();

            WriteFileRequest wr;
            try { wr = _jsonSer.Deserialize<WriteFileRequest>(bodyText); }
            catch (Exception ex) { return "{\"error\":\"invalid json: " + JsonEscape(ex.Message) + "\"}"; }

            if (wr == null || string.IsNullOrEmpty(wr.name))
                return "{\"error\":\"missing required field: name (strategy class name)\"}";
            if (string.IsNullOrEmpty(wr.code))
                return "{\"error\":\"missing required field: code (C# NinjaScript source)\"}";
            if (wr.name.Contains("..") || wr.name.Contains("/") || wr.name.Contains("\\"))
                return "{\"error\":\"name contains invalid path characters\"}";

            string folder = string.IsNullOrEmpty(wr.folder) ? "Strategies" : wr.folder;
            if (folder.Contains("..")) return "{\"error\":\"folder contains path traversal\"}";

            string customDir = NinjaTrader.Core.Globals.UserDataDir + @"bin\Custom\";
            string targetDir = System.IO.Path.Combine(customDir, folder);
            System.IO.Directory.CreateDirectory(targetDir);
            string targetFile = System.IO.Path.Combine(targetDir, wr.name + ".cs");

            try
            {
                System.IO.File.WriteAllText(targetFile, wr.code);
                Log("wrote strategy file: " + targetFile + " (" + wr.code.Length + " chars)");
                return "{\"path\":\"" + JsonEscape(targetFile) + "\",\"size\":" + wr.code.Length + ",\"hint\":\"call POST /compile next\"}";
            }
            catch (Exception ex)
            {
                return "{\"error\":\"write failed: " + JsonEscape(ex.Message) + "\"}";
            }
        }

        private string ReadStrategyFile(string name)
        {
            if (string.IsNullOrEmpty(name) || name.Contains("..") || name.Contains("/") || name.Contains("\\"))
                return "{\"error\":\"invalid name\"}";
            string customDir = NinjaTrader.Core.Globals.UserDataDir + @"bin\Custom\";
            // Search common folders for the file
            foreach (var sub in new[] { "Strategies", "Indicators", "AddOns", "BarsTypes", "DrawingTools" })
            {
                string path = System.IO.Path.Combine(customDir, sub, name + ".cs");
                if (System.IO.File.Exists(path))
                {
                    try
                    {
                        string code = System.IO.File.ReadAllText(path);
                        var sb = new StringBuilder("{");
                        sb.Append("\"name\":\"").Append(JsonEscape(name)).Append("\",");
                        sb.Append("\"folder\":\"").Append(sub).Append("\",");
                        sb.Append("\"path\":\"").Append(JsonEscape(path)).Append("\",");
                        sb.Append("\"size\":").Append(code.Length).Append(",");
                        sb.Append("\"code\":\"").Append(JsonEscape(code)).Append("\"");
                        sb.Append("}");
                        return sb.ToString();
                    }
                    catch (Exception ex) { return "{\"error\":\"read failed: " + JsonEscape(ex.Message) + "\"}"; }
                }
            }
            return "{\"error\":\"file not found\",\"name\":\"" + JsonEscape(name) + "\"}";
        }

        private string CompileNinjaScript(bool fullCompile = false)
        {
            var start = DateTime.Now;
            try
            {
                // NinjaTrader.Code.Compiler.Compile(checkCompileOnly, debugBuild, filesToIgnore, filesInTmp)
                // checkCompileOnly=true → validate only, no DLL overwrite (safe during dev)
                // checkCompileOnly=false → full compile, reloads DLL (needed after deploying new .cs)
                object emitResult = null;
                Exception capturedEx = null;
                try
                {
                    var compilerType = typeof(NinjaTrader.Code.Compiler);
                    var compileMethod = compilerType.GetMethod("Compile", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static);
                    if (compileMethod == null) return "{\"success\":false,\"error\":\"Compile method not found\"}";
                    var compileTask = Task.Run(() => compileMethod.Invoke(null, new object[] {
                        !fullCompile,              // checkCompileOnly: true for check-only, false for full
                        false,                     // debugBuild
                        new List<string>(),        // filesToIgnore
                        new List<string>()         // filesInTmp
                    }));
                    if (!compileTask.Wait(TimeSpan.FromSeconds(120)))
                        return "{\"success\":false,\"error\":\"compile timed out after 120s\"}";
                    emitResult = compileTask.Result;
                }
                catch (Exception ex) { capturedEx = ex; }

                if (capturedEx != null)
                    return "{\"success\":false,\"error\":\"" + JsonEscape(capturedEx.Message + " | " + (capturedEx.InnerException != null ? capturedEx.InnerException.Message : "")) + "\"}";

                var elapsed = (DateTime.Now - start).TotalMilliseconds;

                // EmitResult is Microsoft.CodeAnalysis.Emit.EmitResult (Roslyn)
                // Properties: Success (bool), Diagnostics (ImmutableArray<Diagnostic>)
                var ert = emitResult.GetType();
                bool success = (bool)ert.GetProperty("Success").GetValue(emitResult, null);
                var diagnostics = ert.GetProperty("Diagnostics").GetValue(emitResult, null);

                var sb = new StringBuilder("{");
                sb.Append("\"success\":").Append(success ? "true" : "false").Append(",");
                sb.Append("\"elapsed_ms\":").Append(elapsed.ToString("F0")).Append(",");
                sb.Append("\"diagnostics\":[");
                int errCount = 0, warnCount = 0;
                if (diagnostics != null)
                {
                    var enumerable = diagnostics as System.Collections.IEnumerable;
                    bool first = true;
                    foreach (var diag in enumerable)
                    {
                        var dt = diag.GetType();
                        string severity = dt.GetProperty("Severity").GetValue(diag, null).ToString();
                        string id = (string)dt.GetProperty("Id").GetValue(diag, null);
                        if (severity == "Error") errCount++;
                        else if (severity == "Warning") { warnCount++; if (id == "CS1701") continue; }
                        else continue; // skip info/hidden
                        if (!first) sb.Append(",");
                        string msg = (string)dt.GetMethod("GetMessage", new Type[] { typeof(IFormatProvider) }).Invoke(diag, new object[] { null });
                        var location = dt.GetProperty("Location").GetValue(diag, null);
                        string locStr = "";
                        try
                        {
                            var lineSpan = location.GetType().GetMethod("GetLineSpan").Invoke(location, null);
                            var ls = lineSpan.GetType();
                            string path = (string)ls.GetProperty("Path").GetValue(lineSpan, null);
                            var startPos = ls.GetProperty("StartLinePosition").GetValue(lineSpan, null);
                            int line = (int)startPos.GetType().GetProperty("Line").GetValue(startPos, null) + 1;
                            int col = (int)startPos.GetType().GetProperty("Character").GetValue(startPos, null) + 1;
                            if (!string.IsNullOrEmpty(path))
                            {
                                int lastSlash = System.Math.Max(path.LastIndexOf('\\'), path.LastIndexOf('/'));
                                string file = lastSlash >= 0 ? path.Substring(lastSlash + 1) : path;
                                locStr = file + ":" + line + ":" + col;
                            }
                        }
                        catch { }
                        sb.Append("{");
                        sb.Append("\"severity\":\"").Append(severity).Append("\",");
                        sb.Append("\"id\":\"").Append(id).Append("\",");
                        sb.Append("\"message\":\"").Append(JsonEscape(msg)).Append("\",");
                        sb.Append("\"location\":\"").Append(JsonEscape(locStr)).Append("\"");
                        sb.Append("}");
                        first = false;
                    }
                }
                sb.Append("],");
                sb.Append("\"error_count\":").Append(errCount).Append(",");
                sb.Append("\"warning_count\":").Append(warnCount);
                sb.Append("}");
                Log("compile " + (success ? "success" : "FAILED") + " in " + elapsed.ToString("F0") + "ms (" + errCount + " errors, " + warnCount + " warnings)");
                return sb.ToString();
            }
            catch (Exception ex)
            {
                return "{\"success\":false,\"error\":\"" + JsonEscape(ex.Message) + "\"}";
            }
        }

        // ────── Backtest endpoints ──────

        private string EnqueueBacktest(HttpListenerRequest req)
        {
            string bodyText;
            using (var reader = new StreamReader(req.InputStream, req.ContentEncoding))
                bodyText = reader.ReadToEnd();

            BacktestRequest btr;
            try { btr = _jsonSer.Deserialize<BacktestRequest>(bodyText); }
            catch (Exception ex)
            {
                return "{\"error\":\"invalid json body: " + JsonEscape(ex.Message) + "\"}";
            }

            if (btr == null || string.IsNullOrEmpty(btr.strategy_name))
                return "{\"error\":\"missing required field: strategy_name\"}";
            if (string.IsNullOrEmpty(btr.instrument))
                return "{\"error\":\"missing required field: instrument\"}";
            if (string.IsNullOrEmpty(btr.from_date) || string.IsNullOrEmpty(btr.to_date))
                return "{\"error\":\"missing required fields: from_date, to_date (yyyy-MM-dd)\"}";
            if (string.IsNullOrEmpty(btr.bars_period_type)) btr.bars_period_type = "Minute";
            if (btr.bars_period_value <= 0) btr.bars_period_value = 5;

            var job = new BacktestJob
            {
                id = Guid.NewGuid().ToString("N").Substring(0, 12),
                request = btr,
                status = "queued",
                created = DateTime.Now,
            };
            _jobs[job.id] = job;
            _jobQueue.Add(job);

            Log(string.Format("enqueued backtest {0}: {1} on {2} {3}{4} {5}->{6}",
                job.id, btr.strategy_name, btr.instrument, btr.bars_period_value, btr.bars_period_type,
                btr.from_date, btr.to_date));

            return "{\"id\":\"" + job.id + "\",\"status\":\"queued\",\"poll\":\"/backtest/" + job.id + "\"}";
        }

        private string RunBacktestSync(HttpListenerRequest req)
        {
            // Parse request (same as EnqueueBacktest)
            string bodyText;
            using (var reader = new StreamReader(req.InputStream, req.ContentEncoding))
                bodyText = reader.ReadToEnd();

            BacktestRequest btr;
            try { btr = _jsonSer.Deserialize<BacktestRequest>(bodyText); }
            catch (Exception ex) { return "{\"error\":\"invalid json: " + JsonEscape(ex.Message) + "\"}"; }

            if (btr == null || string.IsNullOrEmpty(btr.strategy_name))
                return "{\"error\":\"missing required field: strategy_name\"}";
            if (string.IsNullOrEmpty(btr.instrument))
                return "{\"error\":\"missing required field: instrument\"}";
            if (string.IsNullOrEmpty(btr.from_date) || string.IsNullOrEmpty(btr.to_date))
                return "{\"error\":\"missing required fields: from_date, to_date\"}";
            if (string.IsNullOrEmpty(btr.bars_period_type)) btr.bars_period_type = "Minute";
            if (btr.bars_period_value <= 0) btr.bars_period_value = 5;

            // Queue and wait
            var job = new BacktestJob
            {
                id = Guid.NewGuid().ToString("N").Substring(0, 12),
                request = btr,
                status = "queued",
                created = DateTime.Now,
            };
            _jobs[job.id] = job;
            _jobQueue.Add(job);
            Log("sync backtest " + job.id + ": " + btr.strategy_name);

            // Block until completed or failed (max 15 min)
            var deadline = DateTime.Now.AddMinutes(15);
            while (DateTime.Now < deadline)
            {
                if (job.status == "completed" || job.status == "failed")
                    break;
                Thread.Sleep(200);
            }

            return GetBacktestJobJson(job.id);
        }

        private string GetBacktestJobJson(string jobId)
        {
            BacktestJob job;
            if (!_jobs.TryGetValue(jobId, out job))
                return "{\"error\":\"job not found\",\"id\":\"" + JsonEscape(jobId) + "\"}";

            var sb = new StringBuilder("{");
            sb.Append("\"id\":\"").Append(job.id).Append("\",");
            sb.Append("\"status\":\"").Append(job.status).Append("\",");
            sb.Append("\"strategy\":\"").Append(JsonEscape(job.request.strategy_name)).Append("\",");
            sb.Append("\"instrument\":\"").Append(JsonEscape(job.request.instrument)).Append("\",");
            sb.Append("\"from\":\"").Append(JsonEscape(job.request.from_date)).Append("\",");
            sb.Append("\"to\":\"").Append(JsonEscape(job.request.to_date)).Append("\",");
            sb.Append("\"created\":\"").Append(job.created.ToString("yyyy-MM-ddTHH:mm:ss")).Append("\",");
            sb.Append("\"elapsed_ms\":").Append(job.elapsed_ms.ToString("F0"));
            if (job.status == "completed" && !string.IsNullOrEmpty(job.results_json))
            {
                sb.Append(",\"results\":").Append(job.results_json);
                sb.Append(",\"completed\":\"").Append(job.completed.Value.ToString("yyyy-MM-ddTHH:mm:ss")).Append("\"");
            }
            if (job.status == "failed" && !string.IsNullOrEmpty(job.error))
                sb.Append(",\"error\":\"").Append(JsonEscape(job.error)).Append("\"");
            sb.Append("}");
            return sb.ToString();
        }

        private string ListBacktestJobsJson()
        {
            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var kv in _jobs)
            {
                if (!first) sb.Append(",");
                sb.Append("{");
                sb.Append("\"id\":\"").Append(kv.Value.id).Append("\",");
                sb.Append("\"status\":\"").Append(kv.Value.status).Append("\",");
                sb.Append("\"strategy\":\"").Append(JsonEscape(kv.Value.request.strategy_name)).Append("\",");
                sb.Append("\"elapsed_ms\":").Append(kv.Value.elapsed_ms.ToString("F0"));
                sb.Append("}");
                first = false;
            }
            sb.Append("]");
            return sb.ToString();
        }

        // ────── Backtest worker (single-threaded, runs jobs sequentially) ──────

        private void BacktestWorkerLoop()
        {
            Log("backtest worker thread started");
            while (!_jobQueue.IsCompleted)
            {
                BacktestJob job;
                try { job = _jobQueue.Take(); }
                catch (InvalidOperationException) { break; }
                catch (Exception ex) { Log("worker take error: " + ex.Message); continue; }

                job.status = "running";
                var start = DateTime.Now;
                try
                {
                    job.results_json = RunBacktest(job.request);
                    job.status = "completed";
                    job.completed = DateTime.Now;
                }
                catch (Exception ex)
                {
                    job.status = "failed";
                    job.error = ex.Message + " | " + (ex.InnerException != null ? ex.InnerException.Message : "");
                    Log("backtest " + job.id + " failed: " + ex.Message);
                }
                job.elapsed_ms = (DateTime.Now - start).TotalMilliseconds;
                Log("backtest " + job.id + " " + job.status + " in " + job.elapsed_ms.ToString("F0") + "ms");
            }
            Log("backtest worker thread exiting");
        }

        // ────── The actual backtest execution (WIP — filled after WPF reflection research) ──────

        private string RunBacktest(BacktestRequest req)
        {
            // Strategy Analyzer WPF automation - in-process, via reflection.
            // Types (all in NinjaTrader.Gui.dll):
            //   NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzer            - the window
            //   NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzerViewModel   - window VM
            //   NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzerTabControl  - tab
            //   NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzerTabProperties - config
            //
            // REQUIRES: a Strategy Analyzer window to be open in NT8 (auto-open TBD in v2).

            // Step 1: find SA window on NT8 main thread (cheap read-only scan)
            var lookupDisp = NinjaTrader.Core.Globals.MainThreadDispatcher;
            if (lookupDisp == null)
                throw new Exception("NT8 MainThreadDispatcher is null");

            object saWindowRef = null;
            lookupDisp.Invoke(new Action(() =>
            {
                foreach (var w in NinjaTrader.Core.Globals.AllWindows)
                {
                    if (w.GetType().FullName == "NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzer")
                    { saWindowRef = w; return; }
                }
            }));
            if (saWindowRef == null)
                throw new Exception("No Strategy Analyzer window open. Open one in NT8 first (New > Strategy Analyzer).");

            // Step 2: all SA UI operations go through THIS window's own Dispatcher
            var disp = ((System.Windows.Threading.DispatcherObject)saWindowRef).Dispatcher;

            string resultJson = null;
            Exception dispatchError = null;
            DateTime? fromDate = null, toDate = null;
            try { fromDate = DateTime.Parse(req.from_date); toDate = DateTime.Parse(req.to_date); }
            catch { throw new Exception("Invalid date format. Use yyyy-MM-dd."); }

            // Setup phase: configure the tab + kick off the run (on SA window's dispatcher)
            object tabObj = null;
            object resultsColl = null;
            int initialResultsCount = 0;
            string initialRunGuid = null;
            object propsRef = null;

            disp.Invoke(new Action(() =>
            {
                try
                {
                    object saWindow = saWindowRef;

                    // 2. ViewModel
                    var vm = saWindow.GetType().GetProperty("ViewModel").GetValue(saWindow, null);

                    // 3. SelectedTab
                    var tab = vm.GetType().GetProperty("SelectedTab").GetValue(vm, null);
                    if (tab == null)
                        throw new Exception("No tab selected in Strategy Analyzer.");
                    tabObj = tab;

                    // 4. TabStrategyProperties
                    var props = tab.GetType().GetProperty("TabStrategyProperties").GetValue(tab, null);
                    if (props == null)
                        throw new Exception("TabStrategyProperties is null on SelectedTab.");

                    // 5. Force strategy re-instantiation by toggling to a different strategy then back.
                    // Without this, SA caches the old compiled instance even after auto-compile.
                    var stratProp = props.GetType().GetProperty("Strategy");
                    string currentStrat = (string)stratProp.GetValue(props, null) ?? "";
                    if (currentStrat == req.strategy_name)
                    {
                        // Toggle to SampleMACrossover (built-in) to force SA to release current instance
                        stratProp.SetValue(props, "SampleMACrossover", null);
                    }
                    stratProp.SetValue(props, req.strategy_name, null);

                    // 6. Instrument
                    props.GetType().GetProperty("InstrumentOrInstrumentList").SetValue(props, req.instrument, null);

                    // 9. StrategyTemplate (holds From/To/parameters)
                    var template = props.GetType().GetProperty("StrategyTemplate").GetValue(props, null);
                    if (template == null)
                        throw new Exception("StrategyTemplate is null - strategy name may be unrecognized: " + req.strategy_name);

                    // 10. Dates on template (StrategyBase.From / StrategyBase.To are DateTime)
                    var fromProp = template.GetType().GetProperty("From");
                    var toProp = template.GetType().GetProperty("To");
                    if (fromProp != null && toProp != null)
                    {
                        fromProp.SetValue(template, fromDate.Value, null);
                        toProp.SetValue(template, toDate.Value, null);
                    }

                    // 11. BarsPeriod on template - direct reference, no reflection
                    try
                    {
                        var bp = new NinjaTrader.Data.BarsPeriod();
                        bp.BarsPeriodType = (NinjaTrader.Data.BarsPeriodType)Enum.Parse(typeof(NinjaTrader.Data.BarsPeriodType), req.bars_period_type, true);
                        bp.Value = req.bars_period_value;
                        // Try property setter first
                        var bpProp = template.GetType().GetProperty("BarsPeriod");
                        bool setOk = false;
                        if (bpProp != null && bpProp.CanWrite)
                        {
                            bpProp.SetValue(template, bp, null);
                            setOk = true;
                        }
                        if (!setOk)
                        {
                            // Walk backing fields in inheritance chain
                            var tt = template.GetType();
                            while (tt != null && !setOk)
                            {
                                foreach (var f in tt.GetFields(System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance))
                                {
                                    if (f.Name.ToLower().Contains("barsperiod") && f.FieldType == typeof(NinjaTrader.Data.BarsPeriod))
                                    {
                                        f.SetValue(template, bp);
                                        setOk = true;
                                        Log("BarsPeriod set via field: " + f.Name);
                                        break;
                                    }
                                }
                                tt = tt.BaseType;
                            }
                        }
                        if (!setOk) Log("BarsPeriod setter not found on StrategyBase - template will use its default period");
                        else Log("BarsPeriod set: " + req.bars_period_type + " " + req.bars_period_value);
                    }
                    catch (Exception bpex) { Log("BarsPeriod set failed: " + bpex.Message); }

                    // 11b. Always enable commission with "Prop Firm" template
                    try
                    {
                        var commProp = template.GetType().GetProperty("IncludeCommission");
                        if (commProp != null && commProp.CanWrite)
                            commProp.SetValue(template, true, null);

                        // Try known property names for commission template
                        string[] commPropNames = { "BacktestCommissionTemplate", "Commission", "CommissionTemplate" };
                        bool commSet = false;
                        foreach (var cn in commPropNames)
                        {
                            var cp = template.GetType().GetProperty(cn);
                            if (cp != null && cp.CanWrite && cp.PropertyType == typeof(string))
                            {
                                cp.SetValue(template, "Prop Firm", null);
                                Log("Commission template set via " + cn + " = Prop Firm");
                                commSet = true;
                                break;
                            }
                        }
                        if (!commSet)
                        {
                            // Dump all string properties to find the right one
                            var allProps = template.GetType().GetProperties();
                            var strProps = new List<string>();
                            foreach (var p in allProps)
                                if (p.PropertyType == typeof(string) && p.Name.ToLower().Contains("comm"))
                                    strProps.Add(p.Name);
                            Log("IncludeCommission=true but no commission template property found. Candidates: " + string.Join(", ", strProps));
                        }
                    }
                    catch (Exception cex) { Log("Commission setup failed: " + cex.Message); }

                    // 12. Apply parameters (if provided)
                    if (req.parameters != null)
                    {
                        foreach (var kv in req.parameters)
                        {
                            var p = template.GetType().GetProperty(kv.Key);
                            if (p != null && p.CanWrite)
                            {
                                try
                                {
                                    var converted = Convert.ChangeType(kv.Value, p.PropertyType);
                                    p.SetValue(template, converted, null);
                                }
                                catch (Exception pex)
                                {
                                    Log("param set failed " + kv.Key + ": " + pex.Message);
                                }
                            }
                        }
                    }

                    // 13. Snapshot state BEFORE running (SelectedRunGuid changes on new run completion)
                    tabObj = tab;
                    propsRef = props;
                    var resultsProp = tab.GetType().GetProperty("Results");
                    resultsColl = resultsProp.GetValue(tab, null);
                    initialRunGuid = (string)props.GetType().GetProperty("SelectedRunGuid").GetValue(props, null) ?? "";

                    // 14. Invoke OnRun (public method on VM)
                    var onRun = vm.GetType().GetMethod("OnRun", new[] { typeof(object), typeof(System.Windows.Input.ExecutedRoutedEventArgs) });
                    if (onRun == null)
                        throw new Exception("OnRun method not found on ViewModel.");
                    onRun.Invoke(vm, new object[] { null, null });
                }
                catch (Exception ex) { dispatchError = ex; }
            }));

            if (dispatchError != null) throw dispatchError;

            // Polling phase: wait for SelectedRunGuid to change AND IsProgressVisible to go false
            var deadline = DateTime.Now.AddMinutes(15);
            bool guidChanged = false;
            bool notRunning = false;
            while (DateTime.Now < deadline)
            {
                Thread.Sleep(500);
                disp.Invoke(new Action(() =>
                {
                    try
                    {
                        var curGuid = (string)propsRef.GetType().GetProperty("SelectedRunGuid").GetValue(propsRef, null) ?? "";
                        if (curGuid != initialRunGuid && !string.IsNullOrEmpty(curGuid)) guidChanged = true;
                        var isProg = (bool)tabObj.GetType().GetProperty("IsProgressVisible").GetValue(tabObj, null);
                        notRunning = !isProg;
                    }
                    catch { }
                }));
                if (guidChanged && notRunning) break;
            }
            if (!guidChanged)
                throw new TimeoutException("Backtest did not complete within 15 minutes (SelectedRunGuid unchanged).");

            // Extraction phase: read SelectedResult (the latest completed entry)
            disp.Invoke(new Action(() =>
            {
                try
                {
                    var selResult = tabObj.GetType().GetProperty("SelectedResult").GetValue(tabObj, null);
                    if (selResult == null)
                    {
                        // Fall back: take last entry in Results collection
                        var countNow = (int)resultsColl.GetType().GetProperty("Count").GetValue(resultsColl, null);
                        if (countNow == 0) throw new Exception("Results collection empty and SelectedResult is null");
                        var indexer = resultsColl.GetType().GetProperty("Item");
                        selResult = indexer.GetValue(resultsColl, new object[] { countNow - 1 });
                    }
                    resultJson = ExtractResultJson(selResult);
                }
                catch (Exception ex) { dispatchError = ex; }
            }));

            if (dispatchError != null) throw dispatchError;
            return resultJson ?? "{\"error\":\"no result extracted\"}";
        }

        private string ExtractResultJson(object gridEntry)
        {
            // StrategyAnalyzerGridEntry has a Strategy (StrategyBase) whose SystemPerformance has the results.
            // Try common property names.
            var et = gridEntry.GetType();
            object strategy = null;
            foreach (var name in new[] { "ResultsStrategy", "CachedParentStrategyClone", "Strategy", "StrategyBase", "StrategyTemplate" })
            {
                var p = et.GetProperty(name);
                if (p != null) { strategy = p.GetValue(gridEntry, null); if (strategy != null) break; }
            }

            SystemPerformance perf = null;
            if (strategy != null)
            {
                var spProp = strategy.GetType().GetProperty("SystemPerformance");
                if (spProp != null) perf = spProp.GetValue(strategy, null) as SystemPerformance;
            }
            if (perf == null)
            {
                // Also check if the entry has SystemPerformance directly
                var spProp = et.GetProperty("SystemPerformance");
                if (spProp != null) perf = spProp.GetValue(gridEntry, null) as SystemPerformance;
            }

            if (perf == null || perf.AllTrades == null || perf.AllTrades.Count == 0)
                return "{\"trades\":0,\"net_profit\":0,\"note\":\"backtest completed but produced no trades or performance data\"}";

            var allTrades = perf.AllTrades;
            var winTrades = allTrades.WinningTrades;
            var loseTrades = allTrades.LosingTrades;
            var currency = allTrades.TradesPerformance.Currency;

            double grossProfit = winTrades.Count > 0 ? winTrades.TradesPerformance.Currency.CumProfit : 0;
            double grossLoss = loseTrades.Count > 0 ? loseTrades.TradesPerformance.Currency.CumProfit : 0;
            double pf = grossLoss != 0 ? Math.Abs(grossProfit / grossLoss) : 0;
            double winRate = allTrades.Count > 0 ? (100.0 * winTrades.Count / allTrades.Count) : 0;
            double avgWin = winTrades.Count > 0 ? grossProfit / winTrades.Count : 0;
            double avgLoss = loseTrades.Count > 0 ? grossLoss / loseTrades.Count : 0;

            // Long/short split
            var longTrades = perf.LongTrades;
            var shortTrades = perf.ShortTrades;
            double longNet = longTrades.Count > 0 ? longTrades.TradesPerformance.Currency.CumProfit : 0;
            double shortNet = shortTrades.Count > 0 ? shortTrades.TradesPerformance.Currency.CumProfit : 0;
            double longGrossProfit = longTrades.WinningTrades.Count > 0 ? longTrades.WinningTrades.TradesPerformance.Currency.CumProfit : 0;
            double longGrossLoss = longTrades.LosingTrades.Count > 0 ? longTrades.LosingTrades.TradesPerformance.Currency.CumProfit : 0;
            double shortGrossProfit = shortTrades.WinningTrades.Count > 0 ? shortTrades.WinningTrades.TradesPerformance.Currency.CumProfit : 0;
            double shortGrossLoss = shortTrades.LosingTrades.Count > 0 ? shortTrades.LosingTrades.TradesPerformance.Currency.CumProfit : 0;
            double longPf = longGrossLoss != 0 ? Math.Abs(longGrossProfit / longGrossLoss) : 0;
            double shortPf = shortGrossLoss != 0 ? Math.Abs(shortGrossProfit / shortGrossLoss) : 0;

            var sb = new StringBuilder("{");
            sb.Append("\"trades\":").Append(allTrades.Count).Append(",");
            sb.Append("\"wins\":").Append(winTrades.Count).Append(",");
            sb.Append("\"losses\":").Append(loseTrades.Count).Append(",");
            sb.Append("\"win_rate_pct\":").Append(winRate.ToString("F2")).Append(",");
            sb.Append("\"net_profit\":").Append(currency.CumProfit.ToString("F2")).Append(",");
            sb.Append("\"gross_profit\":").Append(grossProfit.ToString("F2")).Append(",");
            sb.Append("\"gross_loss\":").Append(grossLoss.ToString("F2")).Append(",");
            sb.Append("\"profit_factor\":").Append(pf.ToString("F3")).Append(",");
            sb.Append("\"avg_win\":").Append(avgWin.ToString("F2")).Append(",");
            sb.Append("\"avg_loss\":").Append(avgLoss.ToString("F2")).Append(",");
            try { sb.Append("\"max_drawdown\":").Append(currency.Drawdown.ToString("F2")).Append(","); } catch { sb.Append("\"max_drawdown\":null,"); }
            try { sb.Append("\"sharpe\":").Append(perf.AllTrades.TradesPerformance.SharpeRatio.ToString("F3")).Append(","); } catch { sb.Append("\"sharpe\":null,"); }
            try { sb.Append("\"sortino\":").Append(perf.AllTrades.TradesPerformance.SortinoRatio.ToString("F3")).Append(","); } catch { sb.Append("\"sortino\":null,"); }
            // Long/short split
            sb.Append("\"long\":{");
            sb.Append("\"trades\":").Append(longTrades.Count).Append(",");
            sb.Append("\"net_profit\":").Append(longNet.ToString("F2")).Append(",");
            sb.Append("\"profit_factor\":").Append(longPf.ToString("F3"));
            sb.Append("},");
            sb.Append("\"short\":{");
            sb.Append("\"trades\":").Append(shortTrades.Count).Append(",");
            sb.Append("\"net_profit\":").Append(shortNet.ToString("F2")).Append(",");
            sb.Append("\"profit_factor\":").Append(shortPf.ToString("F3"));
            sb.Append("}");
            sb.Append("}");
            return sb.ToString();
        }

        // ────── Utilities ──────

        private static string BuildListenUrl()
        {
            int port = DefaultPort;
            try
            {
                string raw = Environment.GetEnvironmentVariable("NT8_HEALTHBRIDGE_PORT");
                int parsed;
                if (!string.IsNullOrEmpty(raw) && int.TryParse(raw, out parsed) && parsed > 0 && parsed < 65536)
                    port = parsed;
            }
            catch { }
            return "http://localhost:" + port + "/";
        }

        private bool InvokeOnMainThreadWithTimeout(Action action, int timeoutMs, out string error)
        {
            error = "";
            var dispatcher = NinjaTrader.Core.Globals.MainThreadDispatcher;
            if (dispatcher == null)
            {
                error = "MainThreadDispatcher unavailable";
                return false;
            }

            Exception captured = null;
            using (var done = new ManualResetEventSlim(false))
            {
                try
                {
                    dispatcher.BeginInvoke(new Action(() =>
                    {
                        try { action(); }
                        catch (Exception ex) { captured = ex; }
                        finally { done.Set(); }
                    }));
                }
                catch (Exception ex)
                {
                    error = "BeginInvoke failed: " + ex.Message;
                    return false;
                }

                if (!done.Wait(timeoutMs))
                {
                    error = "main thread dispatch timed out after " + timeoutMs + "ms";
                    return false;
                }
            }

            if (captured != null)
            {
                error = captured.Message;
                return false;
            }
            return true;
        }

        private bool TryGetPropertyValue(object target, string name, out object value)
        {
            value = null;
            if (target == null || string.IsNullOrEmpty(name))
                return false;
            try
            {
                var p = target.GetType().GetProperty(name);
                if (p == null) return false;
                value = p.GetValue(target, null);
                return true;
            }
            catch { return false; }
        }

        private string GetAnyStringProperty(object target, string[] names)
        {
            if (target == null || names == null) return "";
            foreach (var n in names)
            {
                object v;
                if (TryGetPropertyValue(target, n, out v) && v != null)
                    return v.ToString();
            }
            return "";
        }

        private bool GetAnyBooleanProperty(object target, string[] names)
        {
            if (target == null || names == null) return false;
            foreach (var n in names)
            {
                object v;
                if (!TryGetPropertyValue(target, n, out v) || v == null)
                    continue;
                if (v is bool) return (bool)v;
                bool parsed;
                if (bool.TryParse(v.ToString(), out parsed))
                    return parsed;
            }
            return false;
        }

        private bool InvokeBestEffortNoArg(object target, string[] methodNames, out string error)
        {
            error = "";
            if (target == null)
            {
                error = "target is null";
                return false;
            }
            if (methodNames == null || methodNames.Length == 0)
            {
                error = "no method names supplied";
                return false;
            }

            foreach (var methodName in methodNames)
            {
                try
                {
                    var m = target.GetType().GetMethod(methodName, Type.EmptyTypes);
                    if (m == null) continue;
                    m.Invoke(target, null);
                    return true;
                }
                catch (Exception ex)
                {
                    error = methodName + " failed: " + ex.Message;
                    return false;
                }
            }
            error = "no matching method found";
            return false;
        }

        private List<string> ParseConnectionNames(HttpListenerRequest request)
        {
            var names = new List<string>();
            if (request == null || !request.HasEntityBody)
                return names;
            try
            {
                string bodyText = "";
                using (var reader = new StreamReader(request.InputStream, request.ContentEncoding ?? Encoding.UTF8))
                    bodyText = reader.ReadToEnd();
                if (string.IsNullOrWhiteSpace(bodyText))
                    return names;
                var parsed = _jsonSer.DeserializeObject(bodyText) as Dictionary<string, object>;
                if (parsed == null)
                    return names;

                object rawNames;
                if (!parsed.TryGetValue("connection_names", out rawNames) || rawNames == null)
                    return names;

                var arr = rawNames as System.Collections.ArrayList;
                if (arr != null)
                {
                    foreach (var item in arr)
                    {
                        string value = (item ?? "").ToString().Trim();
                        if (!string.IsNullOrEmpty(value)
                            && !names.Exists(existing => existing.Equals(value, StringComparison.OrdinalIgnoreCase)))
                            names.Add(value);
                    }
                    return names;
                }

                var enumerable = rawNames as System.Collections.IEnumerable;
                if (enumerable != null && !(rawNames is string))
                {
                    foreach (var item in enumerable)
                    {
                        string value = (item ?? "").ToString().Trim();
                        if (!string.IsNullOrEmpty(value)
                            && !names.Exists(existing => existing.Equals(value, StringComparison.OrdinalIgnoreCase)))
                            names.Add(value);
                    }
                    return names;
                }

                var asString = rawNames.ToString();
                if (!string.IsNullOrEmpty(asString))
                {
                    foreach (var token in asString.Split(','))
                    {
                        string value = token.Trim();
                        if (!string.IsNullOrEmpty(value)
                            && !names.Exists(existing => existing.Equals(value, StringComparison.OrdinalIgnoreCase)))
                            names.Add(value);
                    }
                }
            }
            catch (Exception ex)
            {
                Log("recover request parse warning: " + ex.Message);
            }
            return names;
        }

        private bool ConnectionNameMatchesFilter(string connectionName, HashSet<string> filter)
        {
            if (filter == null || filter.Count == 0)
                return true;
            if (string.IsNullOrEmpty(connectionName))
                return false;
            return filter.Contains(connectionName);
        }


        private List<object> GetConfiguredConnectionOptions()
        {
            var options = new List<object>();
            try
            {
                var connType = typeof(Connection);
                foreach (var propertyName in new[] { "Options", "ConnectionOptions", "AvailableConnections" })
                {
                    var prop = connType.GetProperty(propertyName, System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static);
                    if (prop == null)
                        continue;
                    var raw = prop.GetValue(null, null);
                    var enumerable = raw as System.Collections.IEnumerable;
                    if (enumerable == null)
                        continue;
                    foreach (var item in enumerable)
                    {
                        if (item != null)
                            options.Add(item);
                    }
                    if (options.Count > 0)
                        break;
                }
            }
            catch { }
            return options;
        }

        private bool TryConnectConfiguredOption(object option, out string error)
        {
            error = "";
            if (option == null)
            {
                error = "connection option is null";
                return false;
            }
            try
            {
                var connType = typeof(Connection);
                var methods = connType.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                string optionName = GetAnyStringProperty(option, new[] { "Name", "DisplayName", "ConnectionName" });
                foreach (var method in methods)
                {
                    if (!IsConnectMethodName(method.Name))
                        continue;
                    string err;
                    if (TryInvokeConnectMethod(method, null, optionName, option, out err))
                        return true;
                }

                if (TryInvokeConnectMethodsOnTarget(option, optionName, option, out error))
                    return true;

                string ccErr;
                if (TryConnectViaControlCenter(optionName, out ccErr))
                    return true;
                if (!string.IsNullOrEmpty(ccErr))
                    error = string.IsNullOrEmpty(error) ? ccErr : (error + " | " + ccErr);
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
            if (string.IsNullOrEmpty(error))
                error = "no matching static/instance connect method found";
            return false;
        }

        private bool TryConnectConfiguredName(string connectionName, out string error)
        {
            string path;
            return TryConnectConfiguredName(connectionName, out error, out path);
        }

        private bool TryConnectConfiguredName(string connectionName, out string error, out string path)
        {
            error = "";
            path = "";
            if (string.IsNullOrEmpty(connectionName))
            {
                error = "connection name is empty";
                return false;
            }
            try
            {
                // Prefer Control Center UI path — static reflection finds Connect(string) methods
                // that return without throwing but never materialize a runtime Connection.
                string ccErr;
                if (TryConnectViaControlCenter(connectionName, out ccErr))
                {
                    path = string.IsNullOrEmpty(_lastControlCenterPath) ? "control_center" : _lastControlCenterPath;
                    Log("reconnect path: " + path + " succeeded for " + connectionName);
                    return true;
                }
                if (!string.IsNullOrEmpty(ccErr))
                    error = ccErr;

                var connType = typeof(Connection);
                var methods = connType.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                foreach (var method in methods)
                {
                    if (!IsConnectMethodName(method.Name))
                        continue;
                    string err;
                    if (TryInvokeConnectMethod(method, null, connectionName, null, out err))
                        {
                            path = "static:" + method.DeclaringType.Name + "." + method.Name + "(" + method.GetParameters().Length + ")";
                            Log("reconnect path: static_reflection " + method.DeclaringType.FullName + "." + method.Name + " succeeded for " + connectionName);
                            return true;
                        }
                }
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
            if (string.IsNullOrEmpty(error))
                error = "no static connect(string) method found";
            return false;
        }

        private bool TryConnectAnyConfiguredNoArg(out string error)
        {
            error = "";
            try
            {
                var connType = typeof(Connection);
                var methods = connType.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                foreach (var method in methods)
                {
                    if (!IsConnectMethodName(method.Name))
                        continue;
                    if (method.GetParameters().Length == 0)
                    {
                        method.Invoke(null, null);
                        return true;
                    }
                }
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
            error = "no static connect() method found";
            return false;
        }

        private bool TryInvokeConnectMethodsOnTarget(object target, string connectionName, object option, out string error)
        {
            error = "";
            if (target == null)
            {
                error = "connect target is null";
                return false;
            }
            try
            {
                var methods = target.GetType().GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                foreach (var method in methods)
                {
                    if (!IsConnectMethodName(method.Name))
                        continue;
                    string err;
                    if (TryInvokeConnectMethod(method, target, connectionName, option, out err))
                        return true;
                    if (!string.IsNullOrEmpty(err))
                        error = err;
                }
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }

            if (string.IsNullOrEmpty(error))
                error = "no connect/reconnect instance method invocation succeeded";
            return false;
        }

        private bool TryExecuteConnectCommands(object target, string connectionName, out string error)
        {
            error = "";
            if (target == null)
            {
                error = "command target is null";
                return false;
            }
            try
            {
                var props = target.GetType().GetProperties(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                foreach (var prop in props)
                {
                    if (prop == null || !prop.CanRead || prop.PropertyType == null)
                        continue;
                    if (!typeof(System.Windows.Input.ICommand).IsAssignableFrom(prop.PropertyType))
                        continue;
                    if (!IsConnectMethodName(prop.Name))
                        continue;

                    var cmd = prop.GetValue(target, null) as System.Windows.Input.ICommand;
                    if (cmd == null)
                        continue;

                    object preferredParam = string.IsNullOrEmpty(connectionName) ? null : (object)connectionName;
                    if (preferredParam != null && cmd.CanExecute(preferredParam))
                    {
                        cmd.Execute(preferredParam);
                        return true;
                    }
                    if (cmd.CanExecute(null))
                    {
                        cmd.Execute(null);
                        return true;
                    }
                }
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }

            error = "no connect command executed";
            return false;
        }

        private bool IsConnectMethodName(string name)
        {
            if (string.IsNullOrEmpty(name))
                return false;
            if (name.IndexOf("disconnect", StringComparison.OrdinalIgnoreCase) >= 0)
                return false;
            return name.IndexOf("connect", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private string _lastControlCenterPath = "";

        private bool TryConnectViaControlCenter(string connectionName, out string error)
        {
            _lastControlCenterPath = "";
            error = "";
            if (string.IsNullOrEmpty(connectionName))
            {
                error = "connection name is empty";
                return false;
            }
            try
            {
                object controlCenter = null;
                foreach (var w in NinjaTrader.Core.Globals.AllWindows)
                {
                    var fullName = w.GetType().FullName ?? "";
                    if (fullName.IndexOf("ControlCenter", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        controlCenter = w;
                        break;
                    }
                }
                if (controlCenter == null)
                {
                    error = "control center not found";
                    return false;
                }

                var targets = new List<object> { controlCenter };
                object vm;
                if (TryGetPropertyValue(controlCenter, "ViewModel", out vm) && vm != null)
                    targets.Add(vm);

                // Preferred: Connection.Connect(ConnectOptions) — the canonical NT API.
                string apiErr;
                if (TryConnectViaCbiApi(connectionName, out apiErr))
                {
                    _lastControlCenterPath = "cbi_api";
                    return true;
                }
                if (!string.IsNullOrEmpty(apiErr))
                {
                    _lastControlCenterPath = "cbi_api_fail:" + apiErr;
                    error = string.IsNullOrEmpty(error) ? apiErr : (error + " | " + apiErr);
                }

                // Fallback: click the Connections submenu item via reflection.
                string directErr;
                if (TryClickConnectionsSubmenuItem(controlCenter, connectionName, out directErr))
                {
                    _lastControlCenterPath = "cc_direct_click:" + (string.IsNullOrEmpty(_lastDirectClickDetail) ? "ok" : _lastDirectClickDetail);
                    return true;
                }
                if (!string.IsNullOrEmpty(directErr))
                {
                    _lastControlCenterPath = "direct_fail:" + directErr;
                    error = string.IsNullOrEmpty(error) ? directErr : (error + " | " + directErr);
                }

                // Visual-tree menu-click fallback (requires realized visual tree).
                string menuErr;
                if (TryClickConnectionMenuItem(controlCenter, connectionName, out menuErr))
                {
                    _lastControlCenterPath = "cc_menu_click";
                    return true;
                }
                if (!string.IsNullOrEmpty(menuErr))
                {
                    _lastControlCenterPath = "menu_fail:" + menuErr;
                    error = string.IsNullOrEmpty(error) ? menuErr : (error + " | " + menuErr);
                }

                string priorMenuFail = _lastControlCenterPath;
                foreach (var target in targets)
                {
                    string commandErr;
                    if (TryExecuteConnectCommands(target, connectionName, out commandErr))
                    {
                        _lastControlCenterPath = "cc_command:" + target.GetType().Name
                            + (string.IsNullOrEmpty(priorMenuFail) ? "" : " (after " + priorMenuFail + ")");
                        return true;
                    }
                    if (!string.IsNullOrEmpty(commandErr))
                        error = string.IsNullOrEmpty(error) ? commandErr : (error + " | " + commandErr);

                    string targetErr;
                    if (TryInvokeConnectMethodsOnTarget(target, connectionName, null, out targetErr))
                    {
                        _lastControlCenterPath = "cc_instance_method:" + target.GetType().Name
                            + (string.IsNullOrEmpty(priorMenuFail) ? "" : " (after " + priorMenuFail + ")");
                        return true;
                    }
                    if (!string.IsNullOrEmpty(targetErr))
                        error = string.IsNullOrEmpty(error) ? targetErr : (error + " | " + targetErr);
                }
                if (!string.IsNullOrEmpty(menuErr))
                    error = string.IsNullOrEmpty(error) ? menuErr : (error + " | " + menuErr);
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }

            if (string.IsNullOrEmpty(error))
                error = "control center connect invocation failed";
            return false;
        }

        private string _lastDirectClickDetail = "";

        private bool TryConnectViaCbiApi(string connectionName, out string error)
        {
            error = "";
            try
            {
                // Get Core.Globals.ConnectOptions — Collection<ConnectOptions>
                var globalsType = typeof(NinjaTrader.Core.Globals);
                var connOptsProp = globalsType.GetProperty("ConnectOptions", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                if (connOptsProp == null) { error = "Globals.ConnectOptions prop missing"; return false; }
                var connOpts = connOptsProp.GetValue(null, null) as System.Collections.IEnumerable;
                if (connOpts == null) { error = "Globals.ConnectOptions null"; return false; }

                object match = null;
                var available = new List<string>();
                foreach (var opt in connOpts)
                {
                    if (opt == null) continue;
                    string nm = GetAnyStringProperty(opt, new[] { "Name", "DisplayName", "ConnectionName" });
                    if (!string.IsNullOrEmpty(nm)) available.Add(nm);
                    if (string.Equals(nm, connectionName, StringComparison.OrdinalIgnoreCase))
                    {
                        match = opt;
                        break;
                    }
                }
                if (match == null)
                {
                    error = "no ConnectOptions matching '" + connectionName + "' (available: " + string.Join(",", available) + ")";
                    return false;
                }

                // Find Connection.Connect(ConnectOptions) static method
                var connType = typeof(NinjaTrader.Cbi.Connection);
                System.Reflection.MethodInfo connectMethod = null;
                foreach (var m in connType.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static))
                {
                    if (m.Name != "Connect") continue;
                    var ps = m.GetParameters();
                    if (ps.Length == 1 && ps[0].ParameterType.IsAssignableFrom(match.GetType()))
                    {
                        connectMethod = m;
                        break;
                    }
                }
                if (connectMethod == null) { error = "Connection.Connect(ConnectOptions) not found"; return false; }

                connectMethod.Invoke(null, new object[] { match });
                return true;
            }
            catch (Exception ex)
            {
                error = ex.InnerException != null ? ex.InnerException.Message : ex.Message;
                return false;
            }
        }

        private bool TryClickConnectionsSubmenuItem(object controlCenter, string connectionName, out string error)
        {
            error = "";
            _lastDirectClickDetail = "";
            var win = controlCenter as System.Windows.Window;
            if (win == null) { error = "cc not a Window"; return false; }

            object cmi = null;
            try
            {
                var fld = controlCenter.GetType().GetField("connectionsMenuItem",
                    System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance);
                if (fld == null) { error = "connectionsMenuItem field not found"; return false; }
                cmi = fld.GetValue(controlCenter);
                if (cmi == null) { error = "connectionsMenuItem is null"; return false; }
            }
            catch (Exception ex) { error = "cmi_read:" + ex.Message; return false; }

            bool clicked = false;
            string innerErr = "";
            var available = new List<string>();
            try
            {
                win.Dispatcher.Invoke(new Action(() =>
                {
                    try
                    {
                        var itemsProp = cmi.GetType().GetProperty("Items");
                        var items = itemsProp == null ? null : itemsProp.GetValue(cmi, null) as System.Collections.IEnumerable;
                        if (items == null) { innerErr = "Items enumerable null"; return; }
                        string target = NormalizeMenuToken(connectionName);
                        foreach (var child in items)
                        {
                            var mi = child as System.Windows.Controls.MenuItem;
                            if (mi == null) continue;
                            string hdr = GetMenuHeader(mi);
                            if (!string.IsNullOrEmpty(hdr)) available.Add(hdr);
                            if (!NormalizeMenuToken(hdr).Equals(target, StringComparison.Ordinal)) continue;

                            // If DataContext is a ConnectionOptions-like object, invoke its Connect() directly
                            try
                            {
                                var dc = mi.DataContext;
                                if (dc != null)
                                {
                                    var mm = dc.GetType().GetMethod("Connect", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance, null, Type.EmptyTypes, null);
                                    if (mm != null)
                                    {
                                        mm.Invoke(dc, null);
                                        _lastDirectClickDetail = "via_dc_connect";
                                        clicked = true;
                                        return;
                                    }
                                }
                                var tg = mi.Tag;
                                if (tg != null)
                                {
                                    var mm = tg.GetType().GetMethod("Connect", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance, null, Type.EmptyTypes, null);
                                    if (mm != null)
                                    {
                                        mm.Invoke(tg, null);
                                        _lastDirectClickDetail = "via_tag_connect";
                                        clicked = true;
                                        return;
                                    }
                                }
                            }
                            catch (Exception dcex) { innerErr = "dc_connect:" + (dcex.InnerException != null ? dcex.InnerException.Message : dcex.Message); }

                            // Try Command first (clean path)
                            try
                            {
                                if (mi.Command != null && mi.Command.CanExecute(mi.CommandParameter))
                                {
                                    mi.Command.Execute(mi.CommandParameter);
                                    _lastDirectClickDetail = "via_command";
                                    clicked = true;
                                    return;
                                }
                            }
                            catch (Exception cex) { innerErr = "cmd:" + cex.Message; }

                            // Invoke protected MenuItem.OnClick() via reflection — this is what WPF
                            // calls internally when user clicks, and triggers the Click event + handlers.
                            try
                            {
                                var onClick = typeof(System.Windows.Controls.MenuItem).GetMethod("OnClick",
                                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic,
                                    null, Type.EmptyTypes, null);
                                if (onClick != null)
                                {
                                    onClick.Invoke(mi, null);
                                    _lastDirectClickDetail = "via_onclick";
                                    clicked = true;
                                    return;
                                }
                                else
                                {
                                    innerErr = "OnClick method not found";
                                }
                            }
                            catch (Exception ocex) { innerErr = "onclick:" + (ocex.InnerException != null ? ocex.InnerException.Message : ocex.Message); }

                            // Last resort: raise routed event (often ignored by code-behind handlers)
                            try
                            {
                                mi.RaiseEvent(new System.Windows.RoutedEventArgs(System.Windows.Controls.MenuItem.ClickEvent, mi));
                                _lastDirectClickDetail = "via_raise";
                                clicked = true;
                                return;
                            }
                            catch (Exception rex) { innerErr = "raise:" + rex.Message; }
                        }
                    }
                    catch (Exception ex) { innerErr = "iter:" + ex.Message; }
                }));
            }
            catch (Exception ex) { error = "dispatch:" + ex.Message; return false; }

            if (clicked) return true;
            error = string.IsNullOrEmpty(innerErr)
                ? "target not found (available: " + string.Join(",", available) + ")"
                : innerErr;
            return false;
        }

        private bool TryClickConnectionMenuItem(object controlCenter, string connectionName, out string error)
        {
            error = "";
            var root = controlCenter as System.Windows.DependencyObject;
            if (root == null)
            {
                error = "control center visual root unavailable";
                return false;
            }

            var menuItems = new List<System.Windows.Controls.MenuItem>();
            CollectMenuItems(root, menuItems);
            if (menuItems.Count == 0)
            {
                error = "no menu items found";
                return false;
            }

            System.Windows.Controls.MenuItem connectionsMenu = null;
            foreach (var item in menuItems)
            {
                if (NormalizeMenuToken(GetMenuHeader(item)).Equals("connections", StringComparison.Ordinal))
                {
                    connectionsMenu = item;
                    break;
                }
            }
            if (connectionsMenu == null)
            {
                error = "connections menu not found";
                return false;
            }

            string targetToken = NormalizeMenuToken(connectionName);
            var available = new List<string>();
            foreach (var childObj in connectionsMenu.Items)
            {
                var child = childObj as System.Windows.Controls.MenuItem;
                if (child == null)
                    continue;
                string childHeader = GetMenuHeader(child);
                if (!string.IsNullOrEmpty(childHeader))
                    available.Add(childHeader);
                if (!NormalizeMenuToken(childHeader).Equals(targetToken, StringComparison.Ordinal))
                    continue;

                if (!child.IsEnabled)
                {
                    error = "connection menu item disabled: " + childHeader;
                    return false;
                }

                try
                {
                    if (child.Command != null && child.Command.CanExecute(child.CommandParameter))
                    {
                        child.Command.Execute(child.CommandParameter);
                        return true;
                    }
                }
                catch { }

                try
                {
                    child.RaiseEvent(new System.Windows.RoutedEventArgs(System.Windows.Controls.MenuItem.ClickEvent, child));
                    return true;
                }
                catch (Exception ex)
                {
                    error = "menu click failed: " + ex.Message;
                    return false;
                }
            }

            error = "connection menu item not found: " + connectionName
                + (available.Count > 0 ? " (available: " + string.Join(", ", available) + ")" : "");
            return false;
        }

        private void CollectMenuItems(object node, List<System.Windows.Controls.MenuItem> items)
        {
            if (node == null || items == null)
                return;

            var menuItem = node as System.Windows.Controls.MenuItem;
            if (menuItem != null && !items.Contains(menuItem))
                items.Add(menuItem);

            var dep = node as System.Windows.DependencyObject;
            if (dep == null)
                return;

            try
            {
                foreach (var child in System.Windows.LogicalTreeHelper.GetChildren(dep))
                    CollectMenuItems(child, items);
            }
            catch { }

            try
            {
                int childCount = System.Windows.Media.VisualTreeHelper.GetChildrenCount(dep);
                for (int i = 0; i < childCount; i++)
                {
                    var visualChild = System.Windows.Media.VisualTreeHelper.GetChild(dep, i);
                    CollectMenuItems(visualChild, items);
                }
            }
            catch { }
        }

        private string GetMenuHeader(System.Windows.Controls.MenuItem item)
        {
            if (item == null || item.Header == null)
                return "";
            try
            {
                var str = item.Header as string;
                if (!string.IsNullOrEmpty(str))
                    return str.Trim();

                var textProp = item.Header.GetType().GetProperty("Text");
                if (textProp != null)
                {
                    var textValue = textProp.GetValue(item.Header, null);
                    if (textValue != null)
                        return textValue.ToString().Trim();
                }
                return item.Header.ToString().Trim();
            }
            catch
            {
                return "";
            }
        }

        private string NormalizeMenuToken(string value)
        {
            if (string.IsNullOrEmpty(value))
                return "";
            var sb = new StringBuilder();
            foreach (var c in value)
            {
                if (char.IsLetterOrDigit(c))
                    sb.Append(char.ToLowerInvariant(c));
            }
            return sb.ToString();
        }

        private bool TryInvokeConnectMethod(System.Reflection.MethodInfo method, object instance, string connectionName, object option, out string error)
        {
            error = "";
            if (method == null)
            {
                error = "method is null";
                return false;
            }
            try
            {
                var parameters = method.GetParameters();
                var args = new object[parameters.Length];
                var optionType = option != null ? option.GetType() : null;
                for (int i = 0; i < parameters.Length; i++)
                {
                    var p = parameters[i];
                    var t = p.ParameterType;
                    object value = null;
                    bool assigned = false;

                    if (optionType != null && t.IsAssignableFrom(optionType))
                    {
                        value = option;
                        assigned = true;
                    }
                    else if (t == typeof(string))
                    {
                        value = connectionName ?? "";
                        assigned = true;
                    }
                    else if (t == typeof(bool))
                    {
                        value = true;
                        assigned = true;
                    }
                    else if (t.IsValueType)
                    {
                        value = Activator.CreateInstance(t);
                        assigned = true;
                    }
                    else if (!t.IsValueType)
                    {
                        value = null;
                        assigned = true;
                    }

                    if (!assigned)
                    {
                        error = "unsupported parameter type " + t.FullName;
                        return false;
                    }
                    args[i] = value;
                }

                method.Invoke(instance, args);
                return true;
            }
            catch (Exception ex)
            {
                error = ex.InnerException != null ? ex.InnerException.Message : ex.Message;
                return false;
            }
        }

        private List<string> GetConfiguredConnectionNamesFromConfig()
        {
            var names = new List<string>();
            try
            {
                string configPath = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "config.xml");
                if (!File.Exists(configPath))
                    return names;

                var doc = System.Xml.Linq.XDocument.Load(configPath);
                var root = doc.Root;
                if (root == null)
                    return names;
                var connectOptions = root.Element("ConnectOptions");
                if (connectOptions == null)
                    return names;
                foreach (var optionNode in connectOptions.Elements())
                {
                    var nameNode = optionNode.Element("Name");
                    if (nameNode == null)
                        continue;
                    string value = (nameNode.Value ?? "").Trim();
                    if (!string.IsNullOrEmpty(value))
                        names.Add(value);
                }
            }
            catch { }
            return names;
        }

        private string JsonEscape(string s)
        {
            if (s == null) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"")
                    .Replace("\n", "\\n").Replace("\r", "\\r").Replace("\t", "\\t");
        }

        private void Log(string msg)
        {
            try { NinjaTrader.Code.Output.Process("[HealthBridge] " + msg, PrintTo.OutputTab1); }
            catch { }
        }
    }
}
