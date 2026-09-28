using System;
using System.Globalization;
using System.Text;
using cAlgo.API;
using cAlgo.API.Internals;
using IOFile = System.IO.File;
using IODir = System.IO.Directory;
using IOPath = System.IO.Path;

namespace cAlgo.Robots
{
    // Dumps the broker's real contract specifications: commission, swap, tick value,
    // lot size and leverage. These are account-specific, so reading them from the
    // live account beats copying figures off a marketing page.
    [Robot(AccessRights = AccessRights.FullAccess, AddIndicators = false)]
    public class SpecExporter : Robot
    {
        // Every instrument we hold M1 history for. A partial list is still safe - rows
        // for symbols not in this run are preserved - but the full list is the default
        // so a plain run always produces a complete file.
        // Replace this with your own instruments. Run once with no edits to get
        // _symbols.txt from HistoryExporter, which lists the exact names your broker uses.
        [Parameter("Symbols (comma separated)",
                   DefaultValue = "XAUUSD,XAGUSD,US100.cash,BTCUSD")]
        public string SymbolList { get; set; }

        [Parameter("Output folder", DefaultValue = @"C:\ctrader-export")]
        public string OutputFolder { get; set; }

        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        protected override void OnStart()
        {
            IODir.CreateDirectory(OutputFolder);
            const string Header = "symbol,digits,lot_size,pip_size,tick_size,tick_value,"
                + "spread_now,commission,commission_type,swap_long,swap_short,"
                + "swap_calc,volume_min_units,volume_step_units,dynamic_leverage";

            // Merge, never replace. Running this for a handful of symbols must not
            // overwrite the whole file and silently drop every instrument that was not
            // in that run.
            var rows = new System.Collections.Generic.SortedDictionary<string, string>(
                StringComparer.OrdinalIgnoreCase);
            var specPath = IOPath.Combine(OutputFolder, "_specs.csv");
            if (IOFile.Exists(specPath))
            {
                foreach (var line in IOFile.ReadAllLines(specPath))
                {
                    var text = line.Trim();
                    if (text.Length == 0) continue;
                    var comma = text.IndexOf(',');
                    if (comma <= 0) continue;
                    var key = text.Substring(0, comma);
                    if (key == "symbol") continue;          // the header
                    rows[key] = text;
                }
                Print("carried forward " + rows.Count + " existing rows");
            }

            foreach (var raw in SymbolList.Split(','))
            {
                var name = raw.Trim();
                if (name.Length == 0) continue;
                try
                {
                    Symbol s = Symbols.GetSymbol(name);
                    if (s == null) { Print("no symbol: " + name); continue; }

                    string lev = "";
                    try
                    {
                        var dl = s.DynamicLeverage;
                        if (dl != null && dl.Count > 0)
                            lev = dl[0].Leverage.ToString(Inv);
                    }
                    catch (Exception) { lev = "n/a"; }

                    var sb = new StringBuilder();
                    sb.Append(s.Name).Append(',')
                      .Append(s.Digits.ToString(Inv)).Append(',')
                      .Append(s.LotSize.ToString(Inv)).Append(',')
                      .Append(s.PipSize.ToString(Inv)).Append(',')
                      .Append(s.TickSize.ToString(Inv)).Append(',')
                      .Append(s.TickValue.ToString(Inv)).Append(',')
                      .Append(s.Spread.ToString(Inv)).Append(',')
                      .Append(s.Commission.ToString(Inv)).Append(',')
                      .Append(s.CommissionType.ToString()).Append(',')
                      .Append(s.SwapLong.ToString(Inv)).Append(',')
                      .Append(s.SwapShort.ToString(Inv)).Append(',')
                      .Append(s.SwapCalculationType.ToString()).Append(',')
                      .Append(s.VolumeInUnitsMin.ToString(Inv)).Append(',')
                      .Append(s.VolumeInUnitsStep.ToString(Inv)).Append(',')
                      .Append(lev);
                    rows[s.Name] = sb.ToString();

                    Print(name + ": commission=" + s.Commission + " (" + s.CommissionType +
                          "), swapL=" + s.SwapLong + " swapS=" + s.SwapShort +
                          " (" + s.SwapCalculationType + "), spread=" + s.Spread +
                          ", tickValue=" + s.TickValue + ", lot=" + s.LotSize);
                }
                catch (Exception e)
                {
                    Print("FAILED " + name + " -> " + e.Message);
                }
            }

            var outText = new StringBuilder();
            outText.Append(Header).Append('\n');
            foreach (var pair in rows) outText.Append(pair.Value).Append('\n');
            IOFile.WriteAllText(specPath, outText.ToString());
            Print("WROTE " + specPath + " (" + rows.Count + " symbols)");
            Print("=== DONE ===");
            Stop();
        }
    }
}
