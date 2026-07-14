//+------------------------------------------------------------------+
//| BacktestReplayer.mq5  v3.10 (TradeForge / Trading-with-Erik)     |
//| Reads mt5_backtest_SYMBOL.csv from tools/mt5_export.py,          |
//| executes trades with Python-identical H→L bar logic.             |
//|                                                                  |
//| A Trading-with-Erik M1-BELÉPŐket ad → az EA M1 bar-on dolgozik   |
//| (a Trading-with-ai M15-öt adott). SL a pozíción 0, a BE/trail-t  |
//| az EA BELÜL kezeli: minden M1 bar zárásakor Python-azonos H→L:   |
//| előbb TP, majd BE/trail a HIGH-ból, végül SL a LOW-ból.          |
//|                                                                  |
//| Strategy Tester: <SYMBOL> M1, "Open prices only" ajánlott.       |
//| A CSV a Terminal\Common\Files\ mappába kell (FILE_COMMON).       |
//| Modell: az OFF preset (BE + trail). A risky/felező/pajzs +       |
//| kiszállási jel + pozícióépítés Python-oldali (itt nincs).        |
//+------------------------------------------------------------------+
#property copyright "TradeForge"
#property description "Backtest trade executor (M1) — Python H→L bar logic"
#property version   "3.10"
#property strict

#include <Trade\Trade.mqh>

int BarHour(datetime t) { MqlDateTime s; TimeToStruct(t, s); return s.hour; }

//--- Inputs
input string InpCsvFile    = "mt5_backtest_UKOUSD.csv"; // CSV fájlnév (Common\Files\)
input int    InpMagic      = 20260627;                   // Magic number
input int    InpSlippage   = 50;                         // Max slippage (points)
input int    InpEodHour    = 24;                         // EOD zárás óra (24 = KIKAPCS; a Python nem zár EOD-n)
input double InpPipSize    = 0.01;                       // Pip méret (a szimbólumhoz)
input bool   InpShowLines  = true;                       // Vonalak az összes trade-re
input bool   InpShowLabels = true;                       // Szöveg feliratok
input int    InpLineWidth  = 1;                          // Vonalvastagság (1-3)

//--- Object prefix
#define OBJ_PFX   "BTR_"
#define MAX_EVENTS 80000

//--- CSV event (for loading and visualization)
struct TEvent {
    string   type;           // OPEN / SL_MODIFY / CLOSE
    datetime dt;
    string   dir;            // BUY / SELL
    double   price;
    double   sl;
    double   tp;
    double   lot;
    string   comment;
    double   be_trigger;     // absolute price (0 = disabled)
    double   trail_trigger;  // absolute price (0 = disabled)
    double   trail_dist_p;   // pips behind best_price
};

//--- Position state (tracked internally, SL=0 on MT5 position)
struct TPos {
    bool     is_open;
    ulong    ticket;
    string   dir;
    double   entry_price;
    double   sl;             // current SL (managed by EA, NOT set on MT5 position)
    double   tp;
    double   lot;
    double   be_trigger;
    double   trail_trigger;
    double   trail_dist_p;
    double   best_price;
    bool     be_done;
};

//--- Globals
CTrade   g_trade;
TEvent   g_ev[];
int      g_cnt    = 0;
int      g_next   = 0;

TPos     g_pos;
datetime g_last_m15 = 0;  // M15 bar change detection

//+------------------------------------------------------------------+
int OnInit()
{
    g_trade.SetExpertMagicNumber(InpMagic);
    g_trade.SetDeviationInPoints(InpSlippage);
    g_trade.SetTypeFilling(ORDER_FILLING_IOC);

    ZeroMemory(g_pos);
    g_pos.is_open = false;
    g_last_m15    = 0;

    DeleteObjects();
    WritePathHint();

    if(!LoadCSV()) {
        Alert("BacktestReplayer: nem sikerult beolvasni – ", InpCsvFile,
              "\nNezd meg az 'ide_kell_helyezni.txt' fajlt a CSV helyszineert!");
        return INIT_FAILED;
    }

    if(InpShowLines)
        DrawAllTrades();

    ChartRedraw();
    g_next = 0;

    Print("BacktestReplayer v3: ", g_cnt, " esemeny betoltve (H->L bar logic).");
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason) { DeleteObjects(); }

//+------------------------------------------------------------------+
void OnTick()
{
    datetime bar0 = iTime(_Symbol, PERIOD_M1, 0);  // current M15 bar open time

    if(bar0 == g_last_m15) return;  // same bar — nothing to do yet

    // --- New M15 bar started → process the bar that just completed ---
    if(g_last_m15 > 0)
        ProcessBar();

    g_last_m15 = bar0;

    // --- Process CSV OPEN events whose time <= current bar open ---
    // OPEN events are stamped at ts + 15min (bar close = current bar open)
    while(g_next < g_cnt && g_ev[g_next].dt <= bar0)
    {
        TEvent ev = g_ev[g_next];
        g_next++;

        if(ev.type == "OPEN") {
            if(!g_pos.is_open)
                ExecOpen(ev);
            else
                PrintFormat("OPEN eskipped – mar nyitott pozicio (ticket=%I64u)", g_pos.ticket);
        }
        // SL_MODIFY and CLOSE from CSV are ignored — EA handles these internally
    }
}

//+------------------------------------------------------------------+
//| Process the just-completed M15 bar: Python-identical H→L logic   |
//+------------------------------------------------------------------+
void ProcessBar()
{
    // Check if position was auto-closed by MT5 (TP hit)
    if(g_pos.is_open && !PositionSelectByTicket(g_pos.ticket)) {
        PrintFormat("Pozicio auto-zarva TP-n (ticket=%I64u)", g_pos.ticket);
        g_pos.is_open = false;
        return;
    }

    if(!g_pos.is_open) return;

    datetime bar1_t = iTime(_Symbol, PERIOD_M1, 1);   // completed bar open time
    double   hi     = iHigh(_Symbol, PERIOD_M1, 1);
    double   lo     = iLow(_Symbol, PERIOD_M1, 1);
    double   cl     = iClose(_Symbol, PERIOD_M1, 1);
    bool     closed = false;

    if(g_pos.dir == "BUY")
    {
        // 1. TP check FIRST (matches Python order)
        //    (MT5 auto-TP handles this; if auto-close wasn't caught above, check manually)
        if(g_pos.tp > 0 && hi >= g_pos.tp) {
            PrintFormat("BAR-TP: H=%.5f >= tp=%.5f", hi, g_pos.tp);
            ExecBarClose(g_pos.tp, "TP");
            closed = true;
        }
        else {
            // 2. Update best_price from HIGH
            if(hi > g_pos.best_price) g_pos.best_price = hi;

            // 3. BE check (from HIGH, same bar as Python)
            if(g_pos.be_trigger > 0 && !g_pos.be_done && g_pos.best_price >= g_pos.be_trigger) {
                double new_sl = MathMax(g_pos.sl, g_pos.entry_price);
                g_pos.sl      = new_sl;
                g_pos.be_done = true;
                PrintFormat("BE: sl→%.5f (entry=%.5f)", g_pos.sl, g_pos.entry_price);
            }

            // 4. Trail check (from HIGH, same bar as Python)
            if(g_pos.trail_trigger > 0 && g_pos.trail_dist_p > 0 &&
               g_pos.best_price >= g_pos.trail_trigger)
            {
                double trail_sl = g_pos.best_price - g_pos.trail_dist_p * InpPipSize;
                if(trail_sl > g_pos.sl) {
                    g_pos.sl = trail_sl;
                    PrintFormat("TRAIL: best=%.5f → sl=%.5f", g_pos.best_price, g_pos.sl);
                }
            }

            // 5. SL check from LOW (uses updated SL from steps 3+4)
            if(lo <= g_pos.sl) {
                PrintFormat("BAR-SL: L=%.5f <= sl=%.5f [%s]",
                            lo, g_pos.sl, g_pos.be_done ? "BE" : "SL");
                ExecBarClose(g_pos.sl, g_pos.be_done ? "BE" : "SL");
                closed = true;
            }

            // 6. EOD check
            if(!closed && BarHour(bar1_t) >= InpEodHour) {
                PrintFormat("BAR-EOD: hour=%d @ close=%.5f", BarHour(bar1_t), cl);
                ExecBarClose(cl, "EOD");
                closed = true;
            }
        }
    }
    else  // SELL
    {
        // 1. TP check FIRST (for short: LOW hits TP)
        if(g_pos.tp > 0 && lo <= g_pos.tp) {
            PrintFormat("BAR-TP: L=%.5f <= tp=%.5f", lo, g_pos.tp);
            ExecBarClose(g_pos.tp, "TP");
            closed = true;
        }
        else {
            // 2. Update best_price from LOW (for short: best = lowest price seen)
            if(g_pos.best_price == 0)
                g_pos.best_price = lo;
            else
                g_pos.best_price = MathMin(g_pos.best_price, lo);

            // 3. BE check
            if(g_pos.be_trigger > 0 && !g_pos.be_done && g_pos.best_price <= g_pos.be_trigger) {
                double new_sl = MathMin(g_pos.sl, g_pos.entry_price);
                g_pos.sl      = new_sl;
                g_pos.be_done = true;
                PrintFormat("BE: sl→%.5f (entry=%.5f)", g_pos.sl, g_pos.entry_price);
            }

            // 4. Trail check
            if(g_pos.trail_trigger > 0 && g_pos.trail_dist_p > 0 &&
               g_pos.best_price <= g_pos.trail_trigger)
            {
                double trail_sl = g_pos.best_price + g_pos.trail_dist_p * InpPipSize;
                if(trail_sl < g_pos.sl) {
                    g_pos.sl = trail_sl;
                    PrintFormat("TRAIL: best=%.5f → sl=%.5f", g_pos.best_price, g_pos.sl);
                }
            }

            // 5. SL check from HIGH (uses updated SL)
            if(hi >= g_pos.sl) {
                PrintFormat("BAR-SL: H=%.5f >= sl=%.5f [%s]",
                            hi, g_pos.sl, g_pos.be_done ? "BE" : "SL");
                ExecBarClose(g_pos.sl, g_pos.be_done ? "BE" : "SL");
                closed = true;
            }

            // 6. EOD check
            if(!closed && BarHour(bar1_t) >= InpEodHour) {
                PrintFormat("BAR-EOD: hour=%d @ close=%.5f", BarHour(bar1_t), cl);
                ExecBarClose(cl, "EOD");
                closed = true;
            }
        }
    }
}

//+------------------------------------------------------------------+
//| Open position — SL=0 (managed internally), TP set for auto-close |
//+------------------------------------------------------------------+
void ExecOpen(const TEvent &ev)
{
    bool ok = false;
    if(ev.dir == "BUY")
        ok = g_trade.Buy(ev.lot, _Symbol, 0, 0, ev.tp, ev.comment);
    else
        ok = g_trade.Sell(ev.lot, _Symbol, 0, 0, ev.tp, ev.comment);

    if(ok) {
        g_pos.is_open      = true;
        g_pos.ticket       = g_trade.ResultOrder();
        g_pos.dir          = ev.dir;
        g_pos.entry_price  = ev.price;   // Python's entry price (bar close)
        g_pos.sl           = ev.sl;      // initial SL (internal tracking)
        g_pos.tp           = ev.tp;
        g_pos.lot          = ev.lot;
        g_pos.be_trigger   = ev.be_trigger;
        g_pos.trail_trigger= ev.trail_trigger;
        g_pos.trail_dist_p = ev.trail_dist_p;
        g_pos.best_price   = ev.price;
        g_pos.be_done      = false;

        PrintFormat("OPEN %s  lot=%.2f  entry=%.5f  sl=%.5f  tp=%.5f  "
                    "be_trig=%.5f  trail_trig=%.5f  trail_d=%.1f  [%s]  ticket=%I64u",
                    ev.dir, ev.lot, ev.price, ev.sl, ev.tp,
                    ev.be_trigger, ev.trail_trigger, ev.trail_dist_p,
                    ev.comment, g_pos.ticket);
    }
    else {
        PrintFormat("OPEN HIBA (%d): %s", g_trade.ResultRetcode(),
                    g_trade.ResultRetcodeDescription());
    }
}

//+------------------------------------------------------------------+
//| Close position at bar boundary (market price ≈ SL/TP/close)      |
//+------------------------------------------------------------------+
void ExecBarClose(double ref_price, string reason)
{
    if(!g_pos.is_open) return;

    if(g_trade.PositionClose(g_pos.ticket)) {
        PrintFormat("CLOSE [%s]  ref=%.5f  exec=%.5f  ticket=%I64u",
                    reason, ref_price, g_trade.ResultPrice(), g_pos.ticket);
    }
    else {
        // Position might have been auto-closed (TP)
        if(!PositionSelectByTicket(g_pos.ticket))
            PrintFormat("CLOSE [%s] – pozicio mar zarva (auto TP)  ticket=%I64u",
                        reason, g_pos.ticket);
        else
            PrintFormat("CLOSE HIBA (%d): %s",
                        g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
    }
    g_pos.is_open = false;
}

//+------------------------------------------------------------------+
//| Load CSV (12 columns, FILE_COMMON)                               |
//+------------------------------------------------------------------+
bool LoadCSV()
{
    int fh = FileOpen(InpCsvFile, FILE_READ | FILE_CSV | FILE_ANSI | FILE_COMMON, ',');
    if(fh == INVALID_HANDLE) {
        Print("FileOpen hiba (", GetLastError(), "): ", InpCsvFile);
        return false;
    }

    // Skip header row (12 fields)
    for(int c = 0; c < 12 && !FileIsEnding(fh); c++)
        FileReadString(fh);

    ArrayResize(g_ev, MAX_EVENTS);
    g_cnt = 0;

    while(!FileIsEnding(fh) && g_cnt < MAX_EVENTS)
    {
        string type = FileReadString(fh);
        if(type == "" || FileIsEnding(fh)) break;

        string dt_str     = FileReadString(fh);
        string sym        = FileReadString(fh);
        string dir        = FileReadString(fh);
        double price      = StringToDouble(FileReadString(fh));
        double sl         = StringToDouble(FileReadString(fh));
        double tp         = StringToDouble(FileReadString(fh));
        double lot        = StringToDouble(FileReadString(fh));
        string comment    = FileReadString(fh);
        double be_trig    = StringToDouble(FileReadString(fh));
        double trail_trig = StringToDouble(FileReadString(fh));
        double trail_dist = StringToDouble(FileReadString(fh));

        g_ev[g_cnt].type         = type;
        g_ev[g_cnt].dt           = StringToTime(dt_str);
        g_ev[g_cnt].dir          = dir;
        g_ev[g_cnt].price        = price;
        g_ev[g_cnt].sl           = sl;
        g_ev[g_cnt].tp           = tp;
        g_ev[g_cnt].lot          = lot;
        g_ev[g_cnt].comment      = comment;
        g_ev[g_cnt].be_trigger   = be_trig;
        g_ev[g_cnt].trail_trigger= trail_trig;
        g_ev[g_cnt].trail_dist_p = trail_dist;
        g_cnt++;
    }

    FileClose(fh);
    ArrayResize(g_ev, g_cnt);
    Print("LoadCSV: ", g_cnt, " sor betoltve (be/trail parameterekkel).");
    return g_cnt > 0;
}

//+------------------------------------------------------------------+
//| Draw all trades statically on chart (visual preview from OnInit) |
//+------------------------------------------------------------------+
void DrawAllTrades()
{
    int trade_num = 0;

    for(int i = 0; i < g_cnt; i++)
    {
        if(g_ev[i].type != "OPEN") continue;

        datetime open_dt    = g_ev[i].dt;
        double   entry      = g_ev[i].price;
        double   sl0        = g_ev[i].sl;
        double   tp0        = g_ev[i].tp;
        double   lot        = g_ev[i].lot;
        string   dir        = g_ev[i].dir;
        string   profile    = g_ev[i].comment;
        bool     is_buy     = (dir == "BUY");

        datetime close_dt    = open_dt + 3600;
        double   close_price = entry;
        string   result      = "";

        datetime sl_mod_dt[];
        double   sl_mod_val[];
        string   sl_mod_cmt[];
        int      sl_mod_cnt = 0;

        for(int j = i + 1; j < g_cnt; j++)
        {
            if(g_ev[j].type == "OPEN") break;

            if(g_ev[j].type == "SL_MODIFY") {
                ArrayResize(sl_mod_dt,  sl_mod_cnt + 1);
                ArrayResize(sl_mod_val, sl_mod_cnt + 1);
                ArrayResize(sl_mod_cmt, sl_mod_cnt + 1);
                sl_mod_dt[sl_mod_cnt]  = g_ev[j].dt;
                sl_mod_val[sl_mod_cnt] = g_ev[j].sl;
                sl_mod_cmt[sl_mod_cnt] = g_ev[j].comment;
                sl_mod_cnt++;
            }

            if(g_ev[j].type == "CLOSE") {
                close_dt    = g_ev[j].dt;
                close_price = g_ev[j].price;
                result      = g_ev[j].comment;
                break;
            }
        }

        string pfx      = OBJ_PFX + IntegerToString(trade_num) + "_";
        color  col_dir  = is_buy ? clrDodgerBlue : clrOrangeRed;
        color  col_exit = (result == "TP") ? clrLimeGreen :
                          (result == "SL") ? clrCrimson   : clrGray;

        DrawVLine(pfx + "open", open_dt, col_dir, STYLE_SOLID, 1);

        if(InpShowLines && tp0 > 0)
            DrawSegment(pfx + "tp", open_dt, tp0, close_dt, tp0,
                        clrLimeGreen, STYLE_SOLID, InpLineWidth);

        if(InpShowLines) {
            datetime seg_t = open_dt;
            double   seg_s = sl0;

            for(int m = 0; m < sl_mod_cnt; m++) {
                DrawSegment(pfx + "sl" + IntegerToString(m),
                            seg_t, seg_s, sl_mod_dt[m], seg_s,
                            clrCrimson, STYLE_SOLID, InpLineWidth);

                color mod_col = (sl_mod_cmt[m] == "BE") ? clrGold : clrOrange;
                if(sl_mod_cmt[m] == "BE")
                    DrawVLine(pfx + "be" + IntegerToString(m),
                              sl_mod_dt[m], mod_col, STYLE_DASH, 1);

                DrawSegment(pfx + "slmod" + IntegerToString(m),
                            sl_mod_dt[m], seg_s, sl_mod_dt[m], sl_mod_val[m],
                            mod_col, STYLE_SOLID, 1);

                seg_t = sl_mod_dt[m];
                seg_s = sl_mod_val[m];
            }

            DrawSegment(pfx + "sl_fin",
                        seg_t, seg_s, close_dt, seg_s,
                        clrCrimson, STYLE_SOLID, InpLineWidth);
        }

        DrawVLine(pfx + "close", close_dt, col_exit, STYLE_DOT, 1);

        if(InpShowLabels) {
            string lbl = StringFormat("%s %.5f  lot:%.2f  [%s]",
                                      dir, entry, lot, profile);
            DrawText(pfx + "lbl_open", open_dt, is_buy ? tp0 : sl0, lbl, col_dir);

            if(result != "") {
                string lbl2 = StringFormat("%s @ %.5f", result, close_price);
                DrawText(pfx + "lbl_close", close_dt, close_price, lbl2, col_exit);
            }
        }

        trade_num++;
    }

    Print("BacktestReplayer: ", trade_num, " trade kirajzolva (elonezet).");
}

//+------------------------------------------------------------------+
//| Drawing helpers                                                   |
//+------------------------------------------------------------------+
void DrawVLine(string name, datetime dt, color clr, ENUM_LINE_STYLE style, int width)
{
    ObjectDelete(0, name);
    ObjectCreate(0, name, OBJ_VLINE, 0, dt, 0);
    ObjectSetInteger(0, name, OBJPROP_COLOR,      clr);
    ObjectSetInteger(0, name, OBJPROP_STYLE,      style);
    ObjectSetInteger(0, name, OBJPROP_WIDTH,      width);
    ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
    ObjectSetInteger(0, name, OBJPROP_BACK,       true);
}

void DrawSegment(string name,
                 datetime t1, double p1, datetime t2, double p2,
                 color clr, ENUM_LINE_STYLE style, int width)
{
    if(t1 >= t2) return;
    ObjectDelete(0, name);
    ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2);
    ObjectSetInteger(0, name, OBJPROP_COLOR,      clr);
    ObjectSetInteger(0, name, OBJPROP_STYLE,      style);
    ObjectSetInteger(0, name, OBJPROP_WIDTH,      width);
    ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT,  false);
    ObjectSetInteger(0, name, OBJPROP_RAY_LEFT,   false);
    ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
    ObjectSetInteger(0, name, OBJPROP_BACK,       true);
}

void DrawText(string name, datetime dt, double price, string text, color clr)
{
    ObjectDelete(0, name);
    ObjectCreate(0, name, OBJ_TEXT, 0, dt, price);
    ObjectSetString(0,  name, OBJPROP_TEXT,       text);
    ObjectSetInteger(0, name, OBJPROP_COLOR,      clr);
    ObjectSetInteger(0, name, OBJPROP_FONTSIZE,   8);
    ObjectSetString(0,  name, OBJPROP_FONT,       "Courier New");
    ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
    ObjectSetInteger(0, name, OBJPROP_BACK,       false);
}

//+------------------------------------------------------------------+
void WritePathHint()
{
    string common_path = TerminalInfoString(TERMINAL_COMMONDATA_PATH);

    int fh = FileOpen("ide_kell_helyezni.txt", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
    if(fh != INVALID_HANDLE) {
        FileWriteString(fh, "IDE KELL MASOLNI A CSV FAJLT!\r\n");
        FileWriteString(fh, "============================================================\r\n\r\n");
        FileWriteString(fh, "FONTOS: a Strategy Tester minden indulasakor torli a sajat\r\n");
        FileWriteString(fh, "Tester\\Files\\ mappajat! Ezert a CSV-t IDE kell masolni:\r\n\r\n");
        FileWriteString(fh, ">>> " + common_path + "\\Files\\ <<<\r\n\r\n");
        FileWriteString(fh, "Keresett CSV fajl neve:\r\n  " + InpCsvFile + "\r\n\r\n");
        FileWriteString(fh, "Teljes utvonal:\r\n");
        FileWriteString(fh, "  " + common_path + "\\Files\\" + InpCsvFile + "\r\n");
        FileClose(fh);
        Print("CSV helye: ", common_path, "\\Files\\", InpCsvFile);
    }
}

void DeleteObjects()
{
    int total = ObjectsTotal(0);
    for(int i = total - 1; i >= 0; i--) {
        string name = ObjectName(0, i);
        if(StringFind(name, OBJ_PFX) == 0)
            ObjectDelete(0, name);
    }
}
//+------------------------------------------------------------------+
