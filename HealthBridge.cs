#region Using declarations
using System;
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
    /// HealthBridge - HTTP API for NT8 health monitoring and broker reconnect.
    /// Used by the external watchdog to detect stuck UI / broken connections and
    /// to trigger reconnect via NinjaTrader.Cbi.Connection.Connect(ConnectOptions).
    ///
    /// Scope: health + recovery only. Strategy/backtest endpoints live in a separate AddOn.
    ///
    /// Endpoints:
    ///   GET  /health, /healthz, /runtime_snapshot
    ///   GET  /accounts, /positions, /connections, /orders, /trades, /daily_pnl
    ///   POST /recover/reconnect               {"connection_names":[...]}
    ///   POST /recover/flatten_then_reconnect  flatten all positions then reconnect
    ///   POST /strategies/enable_all           SetState(Active) on every non-Active strategy
    ///   POST /compile                         {"full":true} for full recompile (reload DLL)
    /// </summary>
    public class HealthBridge : AddOnBase
    {
        private HttpListener _listener;
        private CancellationTokenSource _cts;
        private Task _serverTask;
        private Thread _heartbeatThread;
        private volatile bool _heartbeatRunning;
        private const int DefaultPort = 8899;
        private readonly string _listenUrl = BuildListenUrl();
        // Bump BuildId whenever editing HealthBridge.cs so the client can detect whether
        // NT is running the freshly-compiled DLL or a stale in-memory AddOn instance.
        // Format: UTC timestamp at edit time.
        private const string BuildId = "2026-04-20T04:45:00Z";
        private static readonly long _startedUtcTicks = DateTime.UtcNow.Ticks;
        private static long _lastRequestUtcTicks = DateTime.UtcNow.Ticks;
        private static long _lastMainThreadTickUtcTicks = DateTime.UtcNow.Ticks;
        private static int _lastMainThreadPingMs = -1;
        private static string _lastMainThreadError = "";
        private static long _lastRecoveryAttemptUtcTicks = 0;
        private static string _lastRecoveryAction = "none";
        private static string _lastRecoveryResult = "none";
        private static string _lastRecoveryError = "";

        private static readonly JavaScriptSerializer _jsonSer = new JavaScriptSerializer();

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
                Interlocked.Exchange(ref _lastRequestUtcTicks, DateTime.UtcNow.Ticks);
                Interlocked.Exchange(ref _lastMainThreadTickUtcTicks, DateTime.UtcNow.Ticks);
                Log("listening on " + _listenUrl);
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

                // POST /compile — recompile NinjaScript so edits to this AddOn take effect
                // without restarting NT. Body: {"full": true} for full compile (reloads DLL),
                // default check-only.
                if (method == "POST" && path == "/compile")
                {
                    bool fullCompile = false;
                    try
                    {
                        string reqBody = new System.IO.StreamReader(ctx.Request.InputStream).ReadToEnd();
                        if (!string.IsNullOrEmpty(reqBody) && reqBody.Contains("\"full\""))
                            fullCompile = reqBody.Contains("\"full\":true") || reqBody.Contains("\"full\": true");
                    }
                    catch { }
                    body = CompileNinjaScript(fullCompile);
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
                else if (method == "POST" && path == "/strategies/enable_all")
                {
                    body = EnableAllStrategiesJson();
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
                        case "/instruments_debug":
                            body = GetInstrumentsDebugJson();
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


        private string GetRuntimeSnapshotJson()
        {
            var sb = new StringBuilder("{");
            sb.Append("\"generated_utc\":\"").Append(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")).Append("\",");
            sb.Append("\"health\":").Append(GetHealthzJson()).Append(",");
            sb.Append("\"connections\":").Append(GetConnectionsJson()).Append(",");
            int blockingCount;
            sb.Append("\"blocking_windows\":").Append(GetBlockingWindowsJson(out blockingCount)).Append(",");
            sb.Append("\"blocking_windows_count\":").Append(blockingCount).Append(",");
            sb.Append("\"accounts\":").Append(GetAccountsJson()).Append(",");
            sb.Append("\"positions\":").Append(GetPositionsJson()).Append(",");
            sb.Append("\"strategy_runtime\":").Append(GetStrategyRuntimeJson());
            sb.Append("}");
            return sb.ToString();
        }

        // Enumerates live strategy instances for read-only status queries
        // (/runtime_snapshot). Probes several known NT internals via reflection so
        // the surface adapts across NT8 builds without hard dependencies.
        // Keys tried, in order:
        //   1. NinjaTrader.Cbi.DB.dbStrategies  (Dictionary<StrategyBase, Operation>)
        //   2. Account.All[*].Strategies         (per-account collection)
        //   3. Globals.AllWindows[*].{ActiveChartControl|ChartControl}.Strategies
        //      (chart-attached StrategyRenderBase)
        // Returns { source, total_count, active_count, strategies[], error }.
        private string GetStrategyRuntimeJson()
        {
            var sb = new StringBuilder("{");
            string source = "";
            var rows = new List<string>();
            int total = 0;
            int active = 0;
            string err = "";

            string dispErr;
            bool dispOk = InvokeOnMainThreadWithTimeout(() =>
            {
                try
                {
                    // 1. DB.dbStrategies — has every registered strategy instance (active or finalized).
                    // Try several common field/property names so we survive small NT version differences.
                    try
                    {
                        var dbType = Type.GetType("NinjaTrader.Cbi.DB, NinjaTrader.Core");
                        System.Collections.IDictionary dict = null;
                        if (dbType != null)
                        {
                            foreach (var memberName in new[] { "dbStrategies", "Strategies", "StrategyMap" })
                            {
                                var fi = dbType.GetField(memberName,
                                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                                if (fi != null)
                                {
                                    dict = fi.GetValue(null) as System.Collections.IDictionary;
                                    if (dict != null && dict.Count > 0) break;
                                }
                                var pi = dbType.GetProperty(memberName,
                                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                                if (pi != null)
                                {
                                    dict = pi.GetValue(null, null) as System.Collections.IDictionary;
                                    if (dict != null && dict.Count > 0) break;
                                }
                            }
                        }
                        if (dict != null && dict.Count > 0)
                        {
                            source = "dbStrategies";
                            foreach (System.Collections.DictionaryEntry kv in dict)
                            {
                                var entry = RenderStrategyEntry(kv.Key);
                                if (entry == null) continue;
                                rows.Add(entry.Item1);
                                total++;
                                if (entry.Item2) active++;
                            }
                        }
                    }
                    catch (Exception ex1) { err = (err.Length == 0 ? "" : err + "; ") + "db:" + ex1.Message; }

                    // 2. Account.All.Strategies
                    if (rows.Count == 0)
                    {
                        try
                        {
                            foreach (var acc in NinjaTrader.Cbi.Account.All)
                            {
                                var stratsProp = acc.GetType().GetProperty("Strategies");
                                if (stratsProp == null) continue;
                                var strats = stratsProp.GetValue(acc, null) as System.Collections.IEnumerable;
                                if (strats == null) continue;
                                foreach (var s in strats)
                                {
                                    var entry = RenderStrategyEntry(s);
                                    if (entry == null) continue;
                                    rows.Add(entry.Item1);
                                    total++;
                                    if (entry.Item2) active++;
                                }
                            }
                            if (rows.Count > 0) source = "account_strategies";
                        }
                        catch (Exception ex2) { err = (err.Length == 0 ? "" : err + "; ") + "acc:" + ex2.Message; }
                    }

                    // 3. Walk chart windows for ChartControl.Strategies
                    if (rows.Count == 0)
                    {
                        try
                        {
                            foreach (var w in NinjaTrader.Core.Globals.AllWindows)
                            {
                                if (w == null) continue;
                                var wType = w.GetType();
                                object cc = null;
                                var activeProp = wType.GetProperty("ActiveChartControl");
                                if (activeProp != null) cc = activeProp.GetValue(w, null);
                                if (cc == null)
                                {
                                    var ccProp = wType.GetProperty("ChartControl");
                                    if (ccProp != null) cc = ccProp.GetValue(w, null);
                                }
                                if (cc == null) continue;
                                var stratsProp = cc.GetType().GetProperty("Strategies");
                                if (stratsProp == null) continue;
                                var strats = stratsProp.GetValue(cc, null) as System.Collections.IEnumerable;
                                if (strats == null) continue;
                                foreach (var s in strats)
                                {
                                    var entry = RenderStrategyEntry(s);
                                    if (entry == null) continue;
                                    rows.Add(entry.Item1);
                                    total++;
                                    if (entry.Item2) active++;
                                }
                            }
                            if (rows.Count > 0) source = "chart_strategies";
                        }
                        catch (Exception ex3) { err = (err.Length == 0 ? "" : err + "; ") + "chart:" + ex3.Message; }
                    }
                }
                catch (Exception ex)
                {
                    err = (err.Length == 0 ? "" : err + "; ") + ex.Message;
                }
            }, 3000, out dispErr);
            if (!dispOk) err = (err.Length == 0 ? "dispatch:" : err + "; dispatch:") + dispErr;

            sb.Append("\"source\":\"").Append(JsonEscape(source)).Append("\",");
            sb.Append("\"total_count\":").Append(total).Append(",");
            sb.Append("\"active_count\":").Append(active).Append(",");
            sb.Append("\"strategies\":[");
            for (int i = 0; i < rows.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append(rows[i]);
            }
            sb.Append("],");
            sb.Append("\"error\":\"").Append(JsonEscape(err)).Append("\"");
            sb.Append("}");
            return sb.ToString();
        }

        // Extracts a small JSON record for one strategy instance via reflection.
        // Returns (jsonObject, isActive) or null when the object isn't
        // strategy-shaped. Designed to not throw — silently skips missing props.
        private Tuple<string, bool> RenderStrategyEntry(object s)
        {
            if (s == null) return null;
            try
            {
                var t = s.GetType();
                string name = "";
                try
                {
                    var n = t.GetProperty("Name");
                    if (n != null)
                    {
                        var v = n.GetValue(s, null);
                        if (v != null) name = v.ToString();
                    }
                }
                catch { }
                if (string.IsNullOrEmpty(name))
                {
                    name = t.Name;
                }

                string stateName = "";
                try
                {
                    var st = t.GetProperty("State");
                    if (st != null)
                    {
                        var v = st.GetValue(s, null);
                        if (v != null) stateName = v.ToString();
                    }
                }
                catch { }

                bool isEnabled = false;
                try
                {
                    var en = t.GetProperty("IsEnabled");
                    if (en != null)
                    {
                        var v = en.GetValue(s, null);
                        if (v is bool) isEnabled = (bool)v;
                    }
                }
                catch { }
                // When IsEnabled isn't present, fall back to treating an Active state as "on".
                bool isActive = isEnabled || (stateName == "Active" || stateName == "Realtime");

                string account = "";
                try
                {
                    var a = t.GetProperty("Account");
                    if (a != null)
                    {
                        var v = a.GetValue(s, null);
                        if (v != null)
                        {
                            var an = v.GetType().GetProperty("Name");
                            var av = an == null ? null : an.GetValue(v, null);
                            account = av == null ? v.ToString() : av.ToString();
                        }
                    }
                }
                catch { }

                var j = new StringBuilder("{");
                j.Append("\"name\":\"").Append(JsonEscape(name)).Append("\",");
                j.Append("\"state\":\"").Append(JsonEscape(stateName)).Append("\",");
                j.Append("\"is_enabled\":").Append(isActive ? "true" : "false").Append(",");
                j.Append("\"account\":\"").Append(JsonEscape(account)).Append("\"");
                j.Append("}");
                return Tuple.Create(j.ToString(), isActive);
            }
            catch
            {
                return null;
            }
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
                        bool invoked = TryConnectConfiguredName(configuredName, out err);
                        if (!invoked && !triedNoArgConnect && targetConnectionNames.Count == 0)
                        {
                            string fallbackErr;
                            bool fallbackInvoked = TryConnectAnyConfiguredNoArg(out fallbackErr);
                            triedNoArgConnect = true;
                            if (fallbackInvoked)
                            {
                                invoked = true;
                                err = "";
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

            string verifyErr;
            bool verifyOk = VerifyConnectionsConnected(out postTotalConnections, out postConnectedConnections, out verifyErr);
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
            sb.Append("\"error\":\"").Append(JsonEscape(opError)).Append("\"");
            sb.Append("}");
            return sb.ToString();
        }

        // Poll Connection.Connections up to 4 times (750ms apart) looking for any connection
        // that reports Connected. Reports the final totals on the last successful dispatch.
        private bool VerifyConnectionsConnected(out int postTotal, out int postConnected, out string lastError)
        {
            postTotal = -1;
            postConnected = -1;
            lastError = "";
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
                    postTotal = localTotal;
                    postConnected = localConnected;
                    if (postConnected > 0)
                        break;
                }
                else
                {
                    lastError = stepErr;
                }
            }
            return verifyOk;
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
            error = "";
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
                    string tag = string.IsNullOrEmpty(_lastControlCenterPath) ? "control_center" : _lastControlCenterPath;
                    Log("reconnect path: " + tag + " succeeded for " + connectionName);
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


        // Spawns a PowerShell that uses UIAutomation to find every unchecked
        // Enabled-checkbox on the CC Strategies grid and fire a real Space keypress
        // via SendInput. TogglePattern.Toggle() only flipped the property without
        // running NT's WPF command binding (so NT reverted); SetFocus + Space routes
        // through the input manager and activates the strategy for real.
        // AutomationId is EnableDisableSingleStrategyCommand (one per strategy row).
        private const string _PS_ENABLE_STRATS_SCRIPT = @"
Add-Type -AssemblyName UIAutomationClient -ErrorAction SilentlyContinue
Add-Type -AssemblyName UIAutomationTypes  -ErrorAction SilentlyContinue

# SendInput SPACE keypress — fires the full WPF command chain bound to the
# checkbox (same as a keyboard Space press by a user). Doesn't require
# foreground window rights like SetCursorPos does, so it works reliably even
# when another window has focus.
$sig = @'
using System;
using System.Runtime.InteropServices;
public static class KbdInput {
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT { public uint type; public InputUnion U; }
    [StructLayout(LayoutKind.Explicit)]
    public struct InputUnion {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public HARDWAREINPUT hi;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)]
    public struct HARDWAREINPUT { public uint uMsg; public ushort wParamL; public ushort wParamH; }
    [DllImport(""user32.dll"")] public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    public const ushort VK_SPACE = 0x20;
    public const uint KEYEVENTF_KEYUP = 0x0002;
    public static void PressSpace() {
        var inputs = new INPUT[2];
        inputs[0].type = 1; // INPUT_KEYBOARD
        inputs[0].U.ki.wVk = VK_SPACE;
        inputs[1].type = 1;
        inputs[1].U.ki.wVk = VK_SPACE;
        inputs[1].U.ki.dwFlags = KEYEVENTF_KEYUP;
        SendInput(2, inputs, Marshal.SizeOf(typeof(INPUT)));
    }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction SilentlyContinue

$auto = [System.Windows.Automation.AutomationElement]
$tree = [System.Windows.Automation.TreeScope]
$cond = New-Object System.Windows.Automation.PropertyCondition($auto::ClassNameProperty, 'ControlCenter')
$win = $auto::RootElement.FindFirst($tree::Children, $cond)
if (-not $win) { exit 2 }

$cbCond = New-Object System.Windows.Automation.PropertyCondition($auto::ControlTypeProperty, [System.Windows.Automation.ControlType]::CheckBox)
$cbs = $win.FindAll($tree::Descendants, $cbCond)
$toggled = 0
$strategyCount = 0
for ($i=0; $i -lt $cbs.Count; $i++) {
    $cb = $cbs.Item($i)
    if ($cb.Current.AutomationId -ne 'EnableDisableSingleStrategyCommand') { continue }
    $strategyCount++
    try {
        $tp = $cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        if ($tp.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On) { continue }

        try { $cb.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView() } catch { }

        $attempt = 0
        while ($attempt -lt 3) {
            $tp = $cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            if ($tp.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On) { break }
            try { $cb.SetFocus() } catch { }
            Start-Sleep -Milliseconds 150
            [KbdInput]::PressSpace()
            Start-Sleep -Seconds (3 + $attempt * 2)
            $attempt++
        }
        $tp = $cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        if ($tp.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On) {
            $toggled++
        }
    } catch { }
}
[Console]::Out.WriteLine(""toggled="" + $toggled + "" count="" + $strategyCount)
";

        private string EnableAllStrategiesJson()
        {
            int toggled = 0;
            int count = 0;
            string errMsg = "";
            try
            {
                // -EncodedCommand expects UTF-16 LE base64 — reliable way to ship a multiline
                // script into powershell.exe without quoting/newline surprises.
                var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(_PS_ENABLE_STRATS_SCRIPT));
                var psi = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = "powershell",
                    Arguments = "-NoProfile -WindowStyle Hidden -EncodedCommand " + encoded,
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                };
                using (var p = System.Diagnostics.Process.Start(psi))
                {
                    if (!p.WaitForExit(60000))
                    {
                        try { p.Kill(); } catch { }
                        errMsg = "powershell timeout";
                    }
                    else
                    {
                        string so = p.StandardOutput.ReadToEnd() ?? "";
                        string se = p.StandardError.ReadToEnd() ?? "";
                        var m = System.Text.RegularExpressions.Regex.Match(so, @"toggled=(\d+) count=(\d+)");
                        if (m.Success)
                        {
                            toggled = int.Parse(m.Groups[1].Value);
                            count = int.Parse(m.Groups[2].Value);
                        }
                        // PowerShell wraps progress records as CLIXML on stderr ("Preparing
                        // modules for first use"). Harmless noise — strip before logging.
                        if (!string.IsNullOrEmpty(se) && !se.TrimStart().StartsWith("#< CLIXML"))
                            errMsg = se.Trim();
                    }
                }
                Log("enable_all_strategies (UIA) toggled=" + toggled + "/" + count + (string.IsNullOrEmpty(errMsg) ? "" : " err=" + errMsg));
            }
            catch (Exception ex)
            {
                errMsg = ex.Message;
                Log("enable_all_strategies (UIA) failed: " + ex.Message);
            }

            var sb0 = new StringBuilder("{");
            sb0.Append("\"method\":\"uia_keypress\",");
            sb0.Append("\"toggled\":").Append(toggled).Append(",");
            sb0.Append("\"checkbox_count\":").Append(count).Append(",");
            sb0.Append("\"error\":\"").Append(JsonEscape(errMsg)).Append("\"");
            sb0.Append("}");
            return sb0.ToString();
        }

        // One-shot diagnostic: probes common NT API surfaces for a subscription /
        // last-tick accessor. Dumps type shapes + any instrument-like entries so
        // we can identify the field to read for tick freshness detection.
        private string GetInstrumentsDebugJson()
        {
            var probes = new List<string>();
            var entries = new List<string>();
            string err;
            bool ok = InvokeOnMainThreadWithTimeout(() =>
            {
                // Look for static "All" style collections on Instrument and MasterInstrument.
                foreach (var typeName in new[] {
                    "NinjaTrader.Cbi.Instrument",
                    "NinjaTrader.Cbi.MasterInstrument",
                    "NinjaTrader.Data.MarketData",
                    "NinjaTrader.Cbi.Subscription",
                })
                {
                    var t = Type.GetType(typeName) ?? Type.GetType(typeName + ", NinjaTrader.Core");
                    if (t == null) { probes.Add(typeName + ":MISSING"); continue; }
                    probes.Add(typeName + ":FOUND");
                    foreach (var p in t.GetProperties(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static))
                    {
                        var pn = p.Name.ToLowerInvariant();
                        if (pn == "all" || pn.Contains("subscrib") || pn.Contains("instance") || pn.Contains("active"))
                            probes.Add(typeName + ".SP:" + p.Name + ":" + p.PropertyType.Name);
                    }
                    foreach (var f in t.GetFields(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static))
                    {
                        var fn = f.Name.ToLowerInvariant();
                        if (fn == "all" || fn.Contains("subscrib") || fn.Contains("instance") || fn.Contains("active"))
                            probes.Add(typeName + ".SF:" + f.Name + ":" + f.FieldType.Name);
                    }
                }

                // Probe Instrument static methods — GetInstrument(string) is the public lookup.
                try
                {
                    var instT2 = Type.GetType("NinjaTrader.Cbi.Instrument, NinjaTrader.Core");
                    if (instT2 != null)
                    {
                        foreach (var m in instT2.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static))
                        {
                            var mn = m.Name;
                            if (mn.StartsWith("Get") || mn.Contains("Find") || mn.Contains("Lookup"))
                                probes.Add("InstS.M:" + mn + "(" + string.Join(",", Array.ConvertAll(m.GetParameters(), pp => pp.ParameterType.Name)) + ")");
                        }
                    }
                }
                catch { }

                // Quick lookup: try to fetch "BTCUSD" specifically to see if it returns MarketData.
                try
                {
                    var instT2 = Type.GetType("NinjaTrader.Cbi.Instrument, NinjaTrader.Core");
                    if (instT2 != null)
                    {
                        var getMethod = instT2.GetMethod("GetInstrument", new Type[] { typeof(string), typeof(bool) });
                        if (getMethod != null)
                        {
                            foreach (var sym in new[] { "BTCUSD", "NQ 06-26" })
                            {
                                var result = getMethod.Invoke(null, new object[] { sym, false });
                                if (result == null)
                                {
                                    probes.Add("GetInstrument('" + sym + "')=null");
                                    continue;
                                }
                                probes.Add("GetInstrument('" + sym + "')=" + result.GetType().Name);
                                object md;
                                if (TryGetPropertyValue(result, "MarketData", out md) && md != null)
                                {
                                    object last;
                                    if (TryGetPropertyValue(md, "Last", out last) && last != null)
                                    {
                                        double lp = 0.0;
                                        DateTime lt = default(DateTime);
                                        try
                                        {
                                            var pp = last.GetType().GetProperty("Price");
                                            if (pp != null) { var pv = pp.GetValue(last, null); if (pv != null) lp = Convert.ToDouble(pv); }
                                            var tp = last.GetType().GetProperty("Time");
                                            if (tp != null) { var tv = tp.GetValue(last, null); if (tv is DateTime) lt = (DateTime)tv; }
                                        }
                                        catch { }
                                        probes.Add(sym + " last=" + lp.ToString("F4") + " time=" + (lt == default(DateTime) ? "default" : lt.ToString("o")));
                                    }
                                    else probes.Add(sym + " Last=null");
                                }
                                else probes.Add(sym + " MarketData=null");
                            }
                        }
                        else probes.Add("GetInstrument(string) not found");
                    }
                }
                catch (Exception ex) { probes.Add("getInstrument_err:" + ex.Message); }

                // Probe Instrument instance fields/props — the subscription marker is on each Instrument.
                try
                {
                    var instT = Type.GetType("NinjaTrader.Cbi.Instrument, NinjaTrader.Core")
                        ?? Type.GetType("NinjaTrader.Cbi.Instrument");
                    if (instT != null)
                    {
                        foreach (var p in instT.GetProperties(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.DeclaredOnly))
                            probes.Add("Inst.P:" + p.Name + ":" + p.PropertyType.Name);
                        foreach (var f in instT.GetFields(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.DeclaredOnly))
                            probes.Add("Inst.F:" + f.Name + ":" + f.FieldType.Name);
                    }
                }
                catch { }

                // Walk subscribedThreads — each worker owns a subscribed Instrument with live data.
                try
                {
                    var instT = Type.GetType("NinjaTrader.Cbi.Instrument, NinjaTrader.Core")
                        ?? Type.GetType("NinjaTrader.Cbi.Instrument");
                    if (instT != null)
                    {
                        var stF = instT.GetField("subscribedThreads",
                            System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                        if (stF != null)
                        {
                            var stVal = stF.GetValue(null);
                            probes.Add("subscribedThreads=" + (stVal == null ? "null" : stVal.GetType().FullName));
                            var en2 = stVal as System.Collections.IEnumerable;
                            if (en2 != null)
                            {
                                int i = 0;
                                var nowUtc = DateTime.UtcNow;
                                foreach (var item in en2)
                                {
                                    i++;
                                    if (item == null) continue;
                                    var ittype = item.GetType();
                                    // Dump all members on first item
                                    if (i == 1)
                                    {
                                        foreach (var pp in ittype.GetProperties(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance))
                                            probes.Add("st.P:" + pp.Name + ":" + pp.PropertyType.Name);
                                        foreach (var ff in ittype.GetFields(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance))
                                            probes.Add("st.F:" + ff.Name + ":" + ff.FieldType.Name);
                                    }
                                    object instRef = null;
                                    foreach (var ff in ittype.GetFields(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance))
                                    {
                                        if (ff.FieldType == typeof(WeakReference))
                                        {
                                            var wr = ff.GetValue(item) as WeakReference;
                                            if (wr != null && wr.IsAlive)
                                            {
                                                var tgt = wr.Target;
                                                if (i <= 2 && tgt != null)
                                                    probes.Add("st[" + i + "].wr_target=" + tgt.GetType().FullName);
                                                instRef = tgt;
                                            }
                                            if (instRef != null) break;
                                        }
                                    }
                                    if (instRef == null) { probes.Add("st[" + i + "]:no_instrument_ref"); continue; }
                                    string nm = GetAnyStringProperty(instRef, new[] { "FullName", "Name" });
                                    // Read Last via MarketData.Last (struct)
                                    double lastPrice = 0.0;
                                    DateTime lastTime = default(DateTime);
                                    object md;
                                    if (TryGetPropertyValue(instRef, "MarketData", out md) && md != null)
                                    {
                                        object last;
                                        if (TryGetPropertyValue(md, "Last", out last) && last != null)
                                        {
                                            try
                                            {
                                                var pp = last.GetType().GetProperty("Price");
                                                if (pp != null) { var pv = pp.GetValue(last, null); if (pv != null) lastPrice = Convert.ToDouble(pv); }
                                                var tp = last.GetType().GetProperty("Time");
                                                if (tp != null) { var tv = tp.GetValue(last, null); if (tv is DateTime) lastTime = (DateTime)tv; }
                                            }
                                            catch { }
                                        }
                                    }
                                    DateTime utcTime = lastTime == default(DateTime) ? default(DateTime) : (lastTime.Kind == DateTimeKind.Utc ? lastTime : lastTime.ToUniversalTime());
                                    double ageSec = lastTime == default(DateTime) ? -1.0 : (nowUtc - utcTime).TotalSeconds;
                                    entries.Add("{\"name\":\"" + JsonEscape(nm)
                                        + "\",\"last_price\":" + lastPrice.ToString("F4")
                                        + ",\"last_time_utc\":\"" + (lastTime == default(DateTime) ? "" : utcTime.ToString("yyyy-MM-ddTHH:mm:ssZ"))
                                        + "\",\"age_sec\":" + ageSec.ToString("F1")
                                        + "}");
                                }
                                probes.Add("subscribedThreads count=" + i);
                            }
                        }
                    }
                }
                catch (Exception ex) { probes.Add("sub_probe_err:" + ex.Message); }

                // Walk Instrument.All; only emit instruments that actually carry tick data
                // (MarketData.Last.Price > 0). Report age of last tick in seconds.
                try
                {
                    var instrumentType = Type.GetType("NinjaTrader.Cbi.Instrument, NinjaTrader.Core")
                        ?? Type.GetType("NinjaTrader.Cbi.Instrument");
                    if (instrumentType != null)
                    {
                        var allProp = instrumentType.GetProperty("All",
                            System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
                        var coll = allProp == null ? null : allProp.GetValue(null, null);
                        var en = coll as System.Collections.IEnumerable;
                        if (en != null)
                        {
                            int total = 0, live = 0;
                            var nowUtc = DateTime.UtcNow;
                            foreach (var inst in en)
                            {
                                if (inst == null) continue;
                                total++;
                                object md;
                                if (!TryGetPropertyValue(inst, "MarketData", out md) || md == null) continue;
                                object lastObj;
                                if (!TryGetPropertyValue(md, "Last", out lastObj) || lastObj == null) continue;
                                // MarketDataEventArgs.Price (double) + Time (DateTime)
                                double lastPrice = 0.0;
                                DateTime lastTime = default(DateTime);
                                try
                                {
                                    var pp = lastObj.GetType().GetProperty("Price");
                                    if (pp != null)
                                    {
                                        var pv = pp.GetValue(lastObj, null);
                                        if (pv != null) lastPrice = Convert.ToDouble(pv);
                                    }
                                    var tp = lastObj.GetType().GetProperty("Time");
                                    if (tp != null)
                                    {
                                        var tv = tp.GetValue(lastObj, null);
                                        if (tv is DateTime) lastTime = (DateTime)tv;
                                    }
                                }
                                catch { }
                                // Filter to actually-subscribed instruments. Instrument.MarketDataStub
                                // is the RealtimeData worker NT attaches when subscription is active.
                                object stub = null;
                                var stubField = inst.GetType().GetField("MarketDataStub",
                                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                                if (stubField != null) stub = stubField.GetValue(inst);
                                if (stub == null) continue;
                                live++;
                                string name = GetAnyStringProperty(inst, new[] { "FullName", "Name" });
                                DateTime utcTime = lastTime.Kind == DateTimeKind.Utc ? lastTime : lastTime.ToUniversalTime();
                                double ageSec = lastTime == default(DateTime) ? -1.0 : (nowUtc - utcTime).TotalSeconds;
                                entries.Add("{\"name\":\"" + JsonEscape(name)
                                    + "\",\"last_price\":" + lastPrice.ToString("F4")
                                    + ",\"last_time_utc\":\"" + (lastTime == default(DateTime) ? "" : utcTime.ToString("yyyy-MM-ddTHH:mm:ssZ"))
                                    + "\",\"age_sec\":" + ageSec.ToString("F1")
                                    + "}");
                            }
                            probes.Add("Instrument.All total=" + total + " live=" + live);
                        }
                    }
                }
                catch (Exception ex) { probes.Add("walk_err:" + ex.Message); }

                // Dump MarketData declared members so we can see the structure.
                try
                {
                    var md = Type.GetType("NinjaTrader.Data.MarketData, NinjaTrader.Core")
                        ?? Type.GetType("NinjaTrader.Data.MarketData");
                    if (md != null)
                    {
                        foreach (var p in md.GetProperties(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.DeclaredOnly))
                            probes.Add("MD.P:" + p.Name + ":" + p.PropertyType.Name);
                    }
                    var mdr = Type.GetType("NinjaTrader.Data.MarketDataEventArgs, NinjaTrader.Core");
                    if (mdr != null)
                    {
                        foreach (var p in mdr.GetProperties(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.DeclaredOnly))
                            probes.Add("MDE.P:" + p.Name + ":" + p.PropertyType.Name);
                    }
                }
                catch (Exception ex) { probes.Add("md_probe_err:" + ex.Message); }
            }, 5000, out err);

            var sb0 = new StringBuilder("{");
            sb0.Append("\"ok\":").Append(ok ? "true" : "false").Append(",");
            sb0.Append("\"error\":\"").Append(JsonEscape(err ?? "")).Append("\",");
            sb0.Append("\"entries\":[").Append(string.Join(",", entries)).Append("],");
            sb0.Append("\"probes\":[");
            for (int i = 0; i < probes.Count; i++)
            {
                if (i > 0) sb0.Append(",");
                sb0.Append("\"").Append(JsonEscape(probes[i])).Append("\"");
            }
            sb0.Append("]}");
            return sb0.ToString();
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
