using System;
using System.Globalization;
using System.Text;
using IOFile = System.IO.File;
using IODir = System.IO.Directory;
using IOPath = System.IO.Path;
using cAlgo.API;

namespace cAlgo.Robots
{
    // Dumps M1 history for several symbols to CSV.
    // Needs FullAccess (it writes files), which cTrader will prompt about on first run.
    [Robot(AccessRights = AccessRights.FullAccess, AddIndicators = false)]
    public class HistoryExporter : Robot
    {
        // Replace with your own instruments. Leave "List all broker symbols first" on for
        // the first run: _symbols.txt then tells you the exact names your broker uses.
        [Parameter("Symbols (comma separated)", DefaultValue = "XAUUSD,US100,BTCUSD")]
        public string SymbolList { get; set; }

        [Parameter("Start date (yyyy-MM-dd)", DefaultValue = "2025-01-01")]
        public string StartDate { get; set; }

        [Parameter("Output folder", DefaultValue = @"C:\ctrader-export")]
        public string OutputFolder { get; set; }

        [Parameter("List all broker symbols first", DefaultValue = true)]
        public bool ListSymbols { get; set; }

        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        protected override void OnStart()
        {
            var start = DateTime.ParseExact(StartDate, "yyyy-MM-dd", Inv);
            IODir.CreateDirectory(OutputFolder);

            // Record the server-time vs UTC offset so we can normalise timestamps later.
            // Deliberately NOT written here: account number and broker name. The engine
            // does not need them, and _meta.txt is the file people hand to someone else
            // along with their price exports.
            var meta = string.Format(Inv,
                "server_time,{0}\nlocal_utc_now,{1}\n",
                Server.Time.ToString("yyyy-MM-dd HH:mm:ss", Inv),
                DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss", Inv));
            IOFile.WriteAllText(IOPath.Combine(OutputFolder, "_meta.txt"), meta);
            Print("Server.Time=" + Server.Time + "  UtcNow=" + DateTime.UtcNow);

            if (ListSymbols)
            {
                var all = new StringBuilder();
                for (int i = 0; i < Symbols.Count; i++) all.Append(Symbols[i]).Append('\n');
                IOFile.WriteAllText(IOPath.Combine(OutputFolder, "_symbols.txt"), all.ToString());
                Print("Broker exposes " + Symbols.Count + " symbols -> _symbols.txt");
            }

            foreach (var raw in SymbolList.Split(','))
            {
                var name = raw.Trim();
                if (name.Length == 0) continue;
                try { Export(name, start); }
                catch (Exception e)
                {
                    Print("FAILED " + name + " -> " + e.Message);
                    Suggest(name);
                }
            }

            Print("=== ALL DONE ===");
            Stop();
        }

        // When a ticker is wrong, show the broker's nearest names instead of just failing.
        private void Suggest(string wanted)
        {
            var key = wanted.Length >= 3 ? wanted.Substring(0, 3).ToUpperInvariant() : wanted.ToUpperInvariant();
            var hits = new StringBuilder();
            for (int i = 0; i < Symbols.Count; i++)
                if (Symbols[i].ToUpperInvariant().Contains(key)) hits.Append(Symbols[i]).Append("  ");
            Print(hits.Length > 0 ? "   did you mean: " + hits : "   no symbol contains '" + key + "'");
        }

        private void Export(string symbolName, DateTime start)
        {
            var bars = MarketData.GetBars(TimeFrame.Minute, symbolName);
            if (bars == null || bars.Count == 0) { Print("no bars for " + symbolName); return; }

            // Pull history backwards until we reach the start date or the broker stops serving.
            int guard = 0;
            while (bars.OpenTimes[0] > start && guard < 20000)
            {
                int loaded = bars.LoadMoreHistory();
                guard++;
                if (loaded == 0) { Print(symbolName + ": broker stopped at " + bars.OpenTimes[0]); break; }
                if (guard % 25 == 0)
                    Print(symbolName + " ... back to " + bars.OpenTimes[0].ToString("yyyy-MM-dd") + " (" + bars.Count + " bars)");
            }

            var sb = new StringBuilder(bars.Count * 64);
            sb.Append("timestamp,open,high,low,close,volume\n");
            int written = 0;
            for (int i = 0; i < bars.Count; i++)
            {
                if (bars.OpenTimes[i] < start) continue;
                sb.Append(bars.OpenTimes[i].ToString("yyyy-MM-dd HH:mm:ss", Inv)).Append(',')
                  .Append(bars.OpenPrices[i].ToString(Inv)).Append(',')
                  .Append(bars.HighPrices[i].ToString(Inv)).Append(',')
                  .Append(bars.LowPrices[i].ToString(Inv)).Append(',')
                  .Append(bars.ClosePrices[i].ToString(Inv)).Append(',')
                  .Append(bars.TickVolumes[i].ToString(Inv)).Append('\n');
                written++;
            }

            var path = IOPath.Combine(OutputFolder, symbolName + "_M1.csv");
            IOFile.WriteAllText(path, sb.ToString());
            Print("WROTE " + symbolName + "  rows=" + written + "  earliest=" + bars.OpenTimes[0]);
        }
    }
}
