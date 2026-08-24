//+------------------------------------------------------------------+
//|                                          VIX75_Confluence.mq5    |
//|      Multi-Timeframe Confluence Signal Indicator                 |
//|      Mirrors vix_core.scoring.ConfluenceScorer + SignalEngine    |
//|                                                                  |
//|  PURPOSE: Visual verification tool. Attach to any M5/M15 chart   |
//|  to see exactly where the platform would generate signals.       |
//|  ML gating (HMM + LightGBM) runs server-side only — arrows here  |
//|  show the rule-based evaluation BEFORE ML filtering, so you may  |
//|  see slightly more arrows than the live system produces.         |
//+------------------------------------------------------------------+
#property copyright "VIX75 Platform"
#property version   "1.00"
#property description "MTF Confluence Signal Verifier"
#property description "Draws S/D zones, BUY/SELL arrows, SL/TP lines."
#property description "Mirrors signal-service confluence rules."

#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2

//--- plot: BUY arrows
#property indicator_label1  "Buy Signal"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrLime
#property indicator_width1  2

//--- plot: SELL arrows
#property indicator_label2  "Sell Signal"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrRed
#property indicator_width2  2

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group "=== HTF Trend ==="
input ENUM_TIMEFRAMES InpHTF_TF          = PERIOD_H4;   // HTF for macro trend
input int             InpEMAFast         = 50;           // EMA Fast period
input int             InpEMASlow         = 200;          // EMA Slow period

input group "=== Indicators (LTF) ==="
input int             InpATRPeriod       = 14;           // ATR period
input int             InpRSIPeriod       = 14;           // RSI period
input int             InpRSIOB           = 70;           // RSI Overbought (BBMA proxy)
input int             InpRSIOS           = 30;           // RSI Oversold (BBMA proxy)
input int             InpADXPeriod       = 14;           // ADX period (regime proxy)
input double          InpADXTrendMin     = 20.0;         // ADX >= this = trending (S1/S2 proxy)

input group "=== Supply/Demand Zones ==="
input double          InpImpulseATRMult  = 1.2;          // Impulse body >= ATR x this
input int             InpMaxBaseCandles  = 4;            // Max base candles before impulse
input double          InpBreakATRBuf     = 0.25;         // Break buffer (x ATR beyond edge)
input int             InpMaxZones        = 12;           // Max active zones tracked
input int             InpZoneLookback    = 500;          // Bars to scan for zone origins

input group "=== Scoring ==="
input double          InpMinScore        = 4.5;          // Min confluence score (out of 6*)
input bool            InpAllowS0Fade     = false;        // Allow counter-trend in range regime
input bool            InpShowZones       = true;         // Draw zone rectangles on chart
input bool            InpShowDashboard   = true;         // Show confluence dashboard comment
input bool            InpShowSLTPLines   = true;         // Draw SL/TP lines for latest signal

input group "=== Risk/Reward ==="
input double          InpSL_ATR_Buffer   = 0.5;          // SL beyond zone edge (x ATR)
input double          InpTP1_RR          = 2.0;          // TP1 risk-reward ratio
input double          InpTP2_RR          = 3.0;          // TP2 risk-reward ratio

input group "=== Visual ==="
input color           InpDemandColor     = clrDarkSlateGray;  // Demand zone fill
input color           InpSupplyColor     = clrIndianRed;     // Supply zone fill
input color           InpBuyArrowColor   = clrLime;           // Buy arrow
input color           InpSellArrowColor  = clrRed;            // Sell arrow
input color           InpSLLineColor     = clrRed;            // SL line
input color           InpTPLineColor     = clrLime;           // TP line
input color           InpEntryLineColor  = clrWhite;          // Entry line

//+------------------------------------------------------------------+
//| Indicator buffers                                                |
//+------------------------------------------------------------------+
double BuyArrowBuffer[];
double SellArrowBuffer[];

//+------------------------------------------------------------------+
//| Handles                                                          |
//+------------------------------------------------------------------+
int h_atr_ltf    = INVALID_HANDLE;
int h_rsi_ltf    = INVALID_HANDLE;
int h_ema50_ltf  = INVALID_HANDLE;
int h_ema50_htf  = INVALID_HANDLE;
int h_ema200_ltf = INVALID_HANDLE;
int h_ema200_htf = INVALID_HANDLE;
int h_adx_ltf    = INVALID_HANDLE;

//+------------------------------------------------------------------+
//| Zone tracking                                                    |
//+------------------------------------------------------------------+
#define MAX_ZONES 24

enum ZONE_STATE
{
   ZONE_FRESH = 0,
   ZONE_TESTED,
   ZONE_MITIGATED,
   ZONE_BROKEN
};

enum ZONE_KIND
{
   ZONE_DEMAND = 0,
   ZONE_SUPPLY
};

struct SDZone
{
   datetime created;
   double   top;
   double   bottom;
   int      kind;        // ZONE_KIND
   int      state;       // ZONE_STATE
   int      touches;
   bool     active;      // still valid for signal generation
};

SDZone g_zones[MAX_ZONES];
int    g_zone_count = 0;

//+------------------------------------------------------------------+
//| Dashboard string builder                                         |
//+------------------------------------------------------------------+
string g_dashboard = "";

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   SetIndexBuffer(0, BuyArrowBuffer, INDICATOR_DATA);
   SetIndexBuffer(1, SellArrowBuffer, INDICATOR_DATA);

   PlotIndexSetInteger(0, PLOT_ARROW, 233);  // up arrow code
   PlotIndexSetInteger(1, PLOT_ARROW, 234);  // down arrow code

   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   IndicatorSetString(INDICATOR_SHORTNAME,
      "VIX75 Confluence (" + EnumToString(InpHTF_TF) + ")");

   // --- create indicator handles ---
   h_atr_ltf    = iATR(_Symbol, _Period, InpATRPeriod);
   h_rsi_ltf    = iRSI(_Symbol, _Period, InpRSIPeriod, PRICE_CLOSE);
   h_ema50_ltf  = iMA(_Symbol, _Period, InpEMAFast, 0, MODE_EMA, PRICE_CLOSE);
   h_ema200_ltf = iMA(_Symbol, _Period, InpEMASlow, 0, MODE_EMA, PRICE_CLOSE);
   h_ema50_htf  = iMA(_Symbol, InpHTF_TF, InpEMAFast, 0, MODE_EMA, PRICE_CLOSE);
   h_ema200_htf = iMA(_Symbol, InpHTF_TF, InpEMASlow, 0, MODE_EMA, PRICE_CLOSE);
   h_adx_ltf    = iADX(_Symbol, _Period, InpADXPeriod);

   if(h_atr_ltf == INVALID_HANDLE || h_rsi_ltf == INVALID_HANDLE ||
      h_ema50_ltf == INVALID_HANDLE || h_ema200_ltf == INVALID_HANDLE ||
      h_ema50_htf == INVALID_HANDLE || h_ema200_htf == INVALID_HANDLE ||
      h_adx_ltf == INVALID_HANDLE)
   {
      Print("ERROR: failed to create indicator handle");
      return INIT_FAILED;
   }

   g_zone_count = 0;
   ArrayInitialize(BuyArrowBuffer, EMPTY_VALUE);
   ArrayInitialize(SellArrowBuffer, EMPTY_VALUE);

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(h_atr_ltf    != INVALID_HANDLE) IndicatorRelease(h_atr_ltf);
   if(h_rsi_ltf    != INVALID_HANDLE) IndicatorRelease(h_rsi_ltf);
   if(h_ema50_ltf  != INVALID_HANDLE) IndicatorRelease(h_ema50_ltf);
   if(h_ema200_ltf != INVALID_HANDLE) IndicatorRelease(h_ema200_ltf);
   if(h_ema50_htf  != INVALID_HANDLE) IndicatorRelease(h_ema50_htf);
   if(h_ema200_htf != INVALID_HANDLE) IndicatorRelease(h_ema200_htf);
   if(h_adx_ltf    != INVALID_HANDLE) IndicatorRelease(h_adx_ltf);

   ObjectsDeleteAll(0, "VIX75_ZONE_");
   ObjectsDeleteAll(0, "VIX75_SLTP_");
   Comment("");
}

//+------------------------------------------------------------------+
//| OnCalculate                                                      |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   int min_bars = InpEMASlow + InpATRPeriod + 10;
   if(rates_total < min_bars) return 0;

   int start = prev_calculated - 1;
   if(start < min_bars) start = min_bars;

   // Process each closed bar
   for(int i = start; i < rates_total; i++)
   {
      BuyArrowBuffer[i]  = EMPTY_VALUE;
      SellArrowBuffer[i] = EMPTY_VALUE;

      // --- Step 1: Update zone states with this bar ---
      UpdateZoneStates(close[i], high[i], low[i], i);

      // --- Step 2: Detect new zones ---
      DetectNewZones(open, high, low, close, i);

      // --- Step 3: Evaluate confluence (only on CLOSED bars, not bar 0) ---
      if(i < rates_total - 1)
      {
         EvaluateBar(i, rates_total, time, open, high, low, close);
      }
   }

   // --- Dashboard update (last bar only) ---
   if(InpShowDashboard && rates_total > 2)
   {
      UpdateDashboard(rates_total - 2, rates_total, time, open, high, low, close);
   }

   return rates_total;
}

//+------------------------------------------------------------------+
//| Read indicator value safely                                      |
//+------------------------------------------------------------------+
double GetIndValue(int handle, int shift)
{
   double buf[1];
   if(CopyBuffer(handle, 0, shift, 1, buf) != 1) return 0.0;
   return buf[0];
}

//+------------------------------------------------------------------+
//| Zone detection: impulse move leaving a base behind               |
//+------------------------------------------------------------------+
void DetectNewZones(const double &open[], const double &high[],
                    const double &low[], const double &close[], int i)
{
   if(i < InpATRPeriod + 2) return;

   double atr = GetIndValue(h_atr_ltf, i);
   if(atr <= 0) return;

   double body_i = MathAbs(close[i] - open[i]);
   double avg_body = 0.0;
   int cnt = MathMin(i, 20);
   for(int j = 0; j < cnt; j++) avg_body += MathAbs(close[i-j] - open[i-j]);
   avg_body /= MathMax(cnt, 1);

   bool is_impulse = body_i > MathMax(InpImpulseATRMult * atr, avg_body * 1.5);
   if(!is_impulse) return;

   // Walk back over base candles
   int base_start = i - 1;
   int base_len = 0;
   while(base_start >= 0 && base_len < InpMaxBaseCandles)
   {
      double body_b = MathAbs(close[base_start] - open[base_start]);
      double avg_b = 0.0;
      int bc = MathMin(base_start, 10);
      for(int j = 0; j < bc; j++) avg_b += MathAbs(close[base_start-1-j] - open[base_start-1-j]);
      avg_b /= MathMax(bc, 1);
      if(body_b > MathMax(avg_b, 0.00001)) break;
      base_start--;
      base_len++;
   }
   base_start++;  // last valid base bar
   if(base_len < 1) return;

   // Determine zone kind from impulse candle direction
   int kind;
   if(close[i] < open[i]) kind = ZONE_SUPPLY;
   else                   kind = ZONE_DEMAND;

   double ztop = high[base_start];
   double zbot = low[base_start];
   for(int j = base_start; j <= i; j++)
   {
      ztop = MathMax(ztop, high[j]);
      zbot = MathMin(zbot, low[j]);
   }

   AddZone(time_stamp(i), ztop, zbot, kind);
}

//+------------------------------------------------------------------+
//| Helper: get timestamp (we need the time[] array passed through)  |
//+------------------------------------------------------------------+
datetime g_last_bar_time = 0;

datetime time_stamp(int i)
{
   // We store the last known time; for accurate timestamps pass time[]
   // This is a simplification for drawing purposes
   return g_last_bar_time;
}

//+------------------------------------------------------------------+
//| Add a zone (deduplicate near-identical zones)                    |
//+------------------------------------------------------------------+
void AddZone(datetime dt, double top, double bottom, int kind)
{
   // Deduplicate: skip if very similar active zone exists
   for(int z = 0; z < g_zone_count; z++)
   {
      if(g_zones[z].kind != kind || !g_zones[z].active) continue;
      if(MathAbs(g_zones[z].top - top) < (g_zones[z].top - g_zones[z].bottom) * 0.3 &&
         MathAbs(g_zones[z].bottom - bottom) < (g_zones[z].top - g_zones[z].bottom) * 0.3)
         return;
   }

   // Shift if full
   if(g_zone_count >= MAX_ZONES)
   {
      for(int z = 0; z < MAX_ZONES - 1; z++) g_zones[z] = g_zones[z + 1];
      g_zone_count = MAX_ZONES - 1;
   }

   g_zones[g_zone_count].created  = dt;
   g_zones[g_zone_count].top      = top;
   g_zones[g_zone_count].bottom   = bottom;
   g_zones[g_zone_count].kind     = kind;
   g_zones[g_zone_count].state    = ZONE_FRESH;
   g_zones[g_zone_count].touches  = 0;
   g_zones[g_zone_count].active   = true;
   g_zone_count++;
}

//+------------------------------------------------------------------+
//| Update zone states with current bar                              |
//+------------------------------------------------------------------+
void UpdateZoneStates(double close_price, double bar_high, double bar_low, int i)
{
   double atr = GetIndValue(h_atr_ltf, i);
   if(atr <= 0) return;
   double buffer = InpBreakATRBuf * atr;

   for(int z = 0; z < g_zone_count; z++)
   {
      if(!g_zones[z].active) continue;
      if(g_zones[z].state == ZONE_BROKEN) { g_zones[z].active = false; continue; }

      bool touched = (bar_high >= g_zones[z].bottom && bar_low <= g_zones[z].top);
      if(!touched) continue;

      g_zones[z].touches++;

      // Break check
      if(g_zones[z].kind == ZONE_DEMAND && close_price < g_zones[z].bottom - buffer)
      {
         g_zones[z].state = ZONE_BROKEN;
         g_zones[z].active = false;
      }
      else if(g_zones[z].kind == ZONE_SUPPLY && close_price > g_zones[z].top + buffer)
      {
         g_zones[z].state = ZONE_BROKEN;
         g_zones[z].active = false;
      }
      else if(g_zones[z].touches == 1 && g_zones[z].state == ZONE_FRESH)
      {
         g_zones[z].state = ZONE_TESTED;
      }
   }
}

//+------------------------------------------------------------------+
//| Get HTF trend from EMA relationship                             |
//+------------------------------------------------------------------+
string GetHTFTrend()
{
   double e50 = GetIndValue(h_ema50_htf, 1);
   double e200 = GetIndValue(h_ema200_htf, 1);
   if(e50 <= 0 || e200 <= 0) return "unknown";
   if(e50 > e200) return "up";
   if(e50 < e200) return "down";
   return "flat";
}

//+------------------------------------------------------------------+
//| Regime proxy using ADX                                           |
//+------------------------------------------------------------------+
int GetRegimeProxy()
{
   // Returns: -1=unknown, 0=S0_range, 1=S1_up, 2=S2_down
   double adx     = GetIndValue(h_adx_ltf, 1, 0);       // main ADX line
   double plus_di = GetIndValue(h_adx_ltf, 1, 1);       // +DI
   double minus_di = GetIndValue(h_adx_ltf, 1, 2);      // -DI

   if(adx <= 0) return -1;
   if(adx < InpADXTrendMin) return 0;  // range regime (S0 proxy)

   if(plus_di > minus_di) return 1;    // up-trend (S1 proxy)
   return 2;                            // down-trend (S2 proxy)
}

//+------------------------------------------------------------------+
//| GetIndValue overload for multi-buffer indicators like ADX       |
//+------------------------------------------------------------------+
double GetIndValue(int handle, int shift, int buffer_index)
{
   double buf[1];
   if(CopyBuffer(handle, buffer_index, shift, 1, buf) != 1) return 0.0;
   return buf[0];
}

//+------------------------------------------------------------------+
//| Main confluence evaluation per bar                               |
//+------------------------------------------------------------------+
void EvaluateBar(int i, int rates_total, const datetime &time[],
                 const double &open[], const double &high[],
                 const double &low[], const double &close[])
{
   double atr = GetIndValue(h_atr_ltf, i);
   double rsi = GetIndValue(h_rsi_ltf, i);
   double ema50 = GetIndValue(h_ema50_ltf, i);
   double ema200 = GetIndValue(h_ema200_ltf, i);
   if(atr <= 0 || rsi <= 0 || ema50 <= 0 || ema200 <= 0) return;

   double bar_close = close[i];

   // ---- Gate 1: Zone touch ----
   int touched_kind = -1;  // -1 = none
   int direction = 0;      // 1 = BUY, -1 = SELL
   for(int z = 0; z < g_zone_count; z++)
   {
      if(!g_zones[z].active) continue;
      if(g_zones[z].state == ZONE_BROKEN || g_zones[z].state == ZONE_MITIGATED) continue;
      if(bar_close < g_zones[z].bottom || bar_close > g_zones[z].top) continue;

      if(g_zones[z].kind == ZONE_DEMAND) { touched_kind = ZONE_DEMAND; direction = 1; }
      else if(g_zones[z].kind == ZONE_SUPPLY) { touched_kind = ZONE_SUPPLY; direction = -1; }
      break;
   }
   if(touched_kind < 0)
   {
      if(InpShowDashboard) StoreRejection("No active zone touch", i);
      return;
   }

   // ---- Gate 2: HTF alignment ----
   string htf = GetHTFTrend();
   bool trend_ok = (direction == 1 && htf == "up") ||
                   (direction == -1 && htf == "down");
   if(!trend_ok && !InpAllowS0Fade)
   {
      if(InpShowDashboard) StoreRejection("HTF misaligned: " + htf + " vs " +
         (direction == 1 ? "BUY" : "SELL"), i);
      return;
   }

   // ---- Gate 3: Regime (ADX proxy) ----
   int regime = GetRegimeProxy();
   if(regime < 0)
   {
      if(InpShowDashboard) StoreRejection("Regime unknown (ADX warming up)", i);
      return;
   }

   bool regime_ok = false;
   string regime_str = "";
   if(direction == 1 && regime == 1) { regime_ok = true; regime_str = "S1 UP"; }
   else if(direction == -1 && regime == 2) { regime_ok = true; regime_str = "S2 DOWN"; }
   else if(regime == 0 && InpAllowS0Fade) { regime_ok = true; regime_str = "S0 FADE"; }

   if(!regime_ok)
   {
      string reg_name = regime == 0 ? "S0 RANGE" : regime == 1 ? "S1 UP" :
                        regime == 2 ? "S2 DOWN" : "?";
      if(InpShowDashboard) StoreRejection(
         "Regime gate: " + reg_name + " blocks " + (direction == 1 ? "BUY" : "SELL"), i);
      return;
   }

   // ---- Component: BBMA proxy ----
   bool bbma_confirms = false;
   if((bar_close >= ema50 && rsi < InpRSIOB) ||
      (bar_close <= ema50 && rsi > InpRSIOS))
      bbma_confirms = true;

   // ---- Scoring (max 6 without meta-label; ML gating is server-side) ----
   int score = 0;
   string breakdown = "";

   // Zone touch (+2)
   score += 2;
   breakdown += "+2 Zone Touch\n";

   // HTF alignment (+1)
   score += 1;
   breakdown += "+1 HTF Aligned (" + htf + ")\n";

   // BBMA (+1)
   if(bbma_confirms) { score += 1; breakdown += "+1 BBMA Confirm\n"; }
   else breakdown += " 0 BBMA (proxy failed)\n";

   // RSI Divergence (0 - not implemented in MT5)
   breakdown += " 0 RSI Div (n/a in MT5)\n";

   // Regime OK (+1)
   score += 1;
   breakdown += "+1 Regime OK (" + regime_str + ")\n";

   // Meta-label (0 - server-side only)
   breakdown += " 0 Meta-Label (cloud only)\n";

   // ---- Threshold check ----
   if(score < (int)MathCeil(InpMinScore))
   {
      if(InpShowDashboard) StoreRejection(
         "Score " + IntegerToString(score) + " < " + DoubleToString(InpMinScore, 0), i);
      return;
   }

   // ---- SIGNAL FIRED! ----
   double entry = bar_close;
   double zone_edge = 0;
   for(int z = 0; z < g_zone_count; z++)
   {
      if(!g_zones[z].active) continue;
      if(g_zones[z].kind == touched_kind &&
         bar_close >= g_zones[z].bottom && bar_close <= g_zones[z].top)
      {
         zone_edge = (touched_kind == ZONE_DEMAND) ? g_zones[z].bottom : g_zones[z].top;
         break;
      }
   }

   double sl, tp1, tp2;
   double risk;
   if(direction == 1)
   {
      sl = zone_edge - InpSL_ATR_Buffer * atr;
      risk = entry - sl;
      tp1 = entry + InpTP1_RR * risk;
      tp2 = entry + InpTP2_RR * risk;
      BuyArrowBuffer[i] = sl;  // arrow below SL level (visible below bar)
   }
   else
   {
      sl = zone_edge + InpSL_ATR_Buffer * atr;
      risk = sl - entry;
      tp1 = entry - InpTP1_RR * risk;
      tp2 = entry - InpTP2_RR * risk;
      SellArrowBuffer[i] = tp1;  // arrow above TP level (visible above bar)
   }

   // Draw SL/TP lines for the LATEST signal only
   if(InpShowSLTPLines && i == rates_total - 2)
   {
      DrawSignalLines(time[i], entry, sl, tp1, direction);
   }

   // Draw zone rectangles
   if(InpShowZones) DrawZoneRectangles();

   // Build dashboard
   if(InpShowDashboard)
   {
      string dir_str = (direction == 1) ? "BUY" : "SELL";
      g_dashboard = StringFormat(
         "\n=== VIX75 CONFLUENCE SIGNAL ===\n"
         "%s | Score: %d/6* (*ML gating is server-side)\n"
         "Entry: %s | SL: %s\nTP1: %s | TP2: %s\n"
         "HTF Trend: %s | Regime: %s\n"
         "--- Confluence Breakdown ---\n%s"
         "==============================\n"
         "Active Zones: %d | Total Tracked: %d",
         dir_str, score,
         DoubleToString(entry, _Digits), DoubleToString(sl, _Digits),
         DoubleToString(tp1, _Digits), DoubleToString(tp2, _Digits),
         htf, regime_str, breakdown,
         CountActiveZones(), g_zone_count
      );
   }
}

//+------------------------------------------------------------------+
//| Store rejection reason for dashboard                             |
//+------------------------------------------------------------------+
string g_last_rejection = "";
int    g_rejection_bar = -1;

void StoreRejection(string reason, int bar_index)
{
   g_last_rejection = reason;
   g_rejection_bar = bar_index;
}

//+------------------------------------------------------------------+
//| Count active zones                                               |
//+------------------------------------------------------------------+
int CountActiveZones()
{
   int count = 0;
   for(int z = 0; z < g_zone_count; z++)
      if(g_zones[z].active && g_zones[z].state != ZONE_BROKEN) count++;
   return count;
}

//+------------------------------------------------------------------+
//| Draw zone rectangles on chart                                    |
//+------------------------------------------------------------------+
void DrawZoneRectangles()
{
   // Remove old zone objects
   ObjectsDeleteAll(0, "VIX75_ZONE_");

   for(int z = 0; z < g_zone_count; z++)
   {
      if(!g_zones[z].active && g_zones[z].state == ZONE_BROKEN) continue;

      string obj_name = "VIX75_ZONE_" + IntegerToString(z);
      datetime t_start = TimeCurrent() - PeriodSeconds(_Period) * InpZoneLookback;
      datetime t_end = TimeCurrent() + PeriodSeconds(_Period) * 20;

      color zone_clr = (g_zones[z].kind == ZONE_DEMAND)
                       ? InpDemandColor : InpSupplyColor;

      if(ObjectFind(0, obj_name) < 0)
      {
         ObjectCreate(0, obj_name, OBJ_RECTANGLE, 0, t_start, g_zones[z].top,
                      t_end, g_zones[z].bottom);
      }
      else
      {
         ObjectMove(0, obj_name, 0, t_start, g_zones[z].top);
         ObjectMove(0, obj_name, 1, t_end, g_zones[z].bottom);
      }
      ObjectSetInteger(0, obj_name, OBJPROP_COLOR, zone_clr);
      ObjectSetInteger(0, obj_name, OBJPROP_FILL, true);
      ObjectSetInteger(0, obj_name, OBJPROP_BACK, true);
      ObjectSetInteger(0, obj_name, OBJPROP_SELECTABLE, false);

      // Label
      string lbl_name = obj_name + "_lbl";
      if(ObjectFind(0, lbl_name) < 0)
      {
         ObjectCreate(0, lbl_name, OBJ_TEXT, 0, t_start, g_zones[z].top);
      }
      else
      {
         ObjectMove(0, lbl_name, 0, t_start, g_zones[z].top);
      }
      string lbl_text = (g_zones[z].kind == ZONE_DEMAND ? "DEMAND" : "SUPPLY");
      lbl_text += " [" + ZoneStateStr(g_zones[z].state) + "]";
      ObjectSetString(0, lbl_name, OBJPROP_TEXT, lbl_text);
      ObjectSetInteger(0, lbl_name, OBJPROP_COLOR, zone_clr);
      ObjectSetInteger(0, lbl_name, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, lbl_name, OBJPROP_SELECTABLE, false);
   }
}

//+------------------------------------------------------------------+
//| Draw SL/TP lines                                                 |
//+------------------------------------------------------------------+
void DrawSignalLines(datetime signal_time, double entry, double sl,
                     double tp1, int direction)
{
   ObjectsDeleteAll(0, "VIX75_SLTP_");

   datetime t_end = TimeCurrent() + PeriodSeconds(_Period) * 30;

   string names[3] = {"VIX75_SLTP_entry", "VIX75_SLTP_sl", "VIX75_SLTP_tp"};
   double vals[3] = {entry, sl, tp1};
   color clrs[3] = {InpEntryLineColor, InpSLLineColor, InpTPLineColor};
   ENUM_LINE_STYLE styles[3] = {STYLE_SOLID, STYLE_DASH, STYLE_DOT};

   for(int i = 0; i < 3; i++)
   {
      if(ObjectFind(0, names[i]) < 0)
      {
         ObjectCreate(0, names[i], OBJ_TREND, 0, signal_time, vals[i], t_end, vals[i]);
      }
      else
      {
         ObjectMove(0, names[i], 0, signal_time, vals[i]);
         ObjectMove(0, names[i], 1, t_end, vals[i]);
      }
      ObjectSetInteger(0, names[i], OBJPROP_COLOR, clrs[i]);
      ObjectSetInteger(0, names[i], OBJPROP_STYLE, styles[i]);
      ObjectSetInteger(0, names[i], OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, names[i], OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, names[i], OBJPROP_SELECTABLE, false);
   }
}

//+------------------------------------------------------------------+
//| Zone state string                                                |
//+------------------------------------------------------------------+
string ZoneStateStr(int state)
{
   switch(state)
   {
      case ZONE_FRESH:     return "FRESH";
      case ZONE_TESTED:    return "TESTED";
      case ZONE_MITIGATED: return "MITIGATED";
      case ZONE_BROKEN:    return "BROKEN";
   }
   return "?";
}

//+------------------------------------------------------------------+
//| Update dashboard comment                                         |
//+------------------------------------------------------------------+
void UpdateDashboard(int i, int rates_total, const datetime &time[],
                     const double &open[], const double &high[],
                     const double &low[], const double &close[])
{
   if(!InpShowDashboard) return;

   string htf = GetHTFTrend();
   int regime = GetRegimeProxy();
   string regime_name = regime == 0 ? "S0 RANGE (range)" :
                        regime == 1 ? "S1 UP-TREND" :
                        regime == 2 ? "S2 DOWN-TREND" : "WARMING UP";

   string dash = "\n";
   dash += "============================================\n";
   dash += "  VIX75 CONFLUENCE INDICATOR\n";
   dash += "  (Rule-based verification - ML gating is cloud-side)\n";
   dash += "============================================\n";
   dash += "  HTF Trend (" + EnumToString(InpHTF_TF) + "): " + htf + "\n";
   dash += "  Regime Proxy (ADX): " + regime_name + "\n";
   dash += "  Active S/D Zones: " + IntegerToString(CountActiveZones()) + "\n";
   dash += "--------------------------------------------\n";

   if(g_dashboard != "" && g_rejection_bar < i)
   {
      dash += g_dashboard;
   }
   else if(g_last_rejection != "" && g_rejection_bar >= i - 3)
   {
      dash += "  LAST REJECTION: " + g_last_rejection + "\n";
   }
   else
   {
      dash += "  Waiting for setup...\n";
   }

   dash += "============================================\n";
   dash += "  NOTE: Arrows show rule-based signals.\n";
   dash += "  Production adds ML gating (HMM+LightGBM)\n";
   dash += "  which filters further. Fewer live signals\n";
   dash += "  than shown here is EXPECTED.\n";
   dash += "============================================\n";

   Comment(dash);
}
