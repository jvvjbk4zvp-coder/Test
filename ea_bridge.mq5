//+------------------------------------------------------------------+
//|                                                  ea_bridge.mq5    |
//|  Polls a JSON command file dropped by the Telegram bot and       |
//|  executes: SL_TO_BE, PARTIAL_CLOSE (25/50/75%), CLOSE_ALL, DERISK|
//|                                                                    |
//|  SETUP                                                             |
//|  1. Copy this file into MQL5/Experts/                             |
//|  2. The bot writes commands/<account_id>.json into the bot's      |
//|     working folder. Point CommandFilePath below at that same      |
//|     file — easiest via a shared folder, or by running a tiny      |
//|     sync (e.g. the bot writes directly into the terminal's        |
//|     MQL5/Files/ directory for this account, which MT5 can read    |
//|     without extra permissions).                                   |
//|  3. Attach to any chart, enable AutoTrading, enable                |
//|     "Allow file access" in EA settings if using local file mode.  |
//+------------------------------------------------------------------+
#property strict

input string CommandFileName   = "ea_commands.json"; // filename inside MQL5/Files/
input int    PollIntervalSecs  = 2;                  // how often to check for commands
input ulong  MagicFilterZero   = 0;                  // 0 = manage ALL positions regardless of magic

datetime lastCheck = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(PollIntervalSecs);
   Print("EA Bridge initialized. Watching file: ", CommandFileName);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
//| Timer: poll the command file                                     |
//+------------------------------------------------------------------+
void OnTimer()
  {
   if(!FileIsExist(CommandFileName, FILE_COMMON) && !FileIsExist(CommandFileName))
      return;

   int flags = FILE_READ | FILE_TXT | FILE_ANSI;
   int handle = FileOpen(CommandFileName, flags);
   if(handle == INVALID_HANDLE)
      return;

   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle) + "\n";
   FileClose(handle);

   if(StringLen(content) < 2)
      return;

   // NOTE: This is a minimal hand-rolled JSON array scanner suitable for
   // the simple, flat structure the bot writes. For production, use a
   // proper JSON library (e.g. the "JAson" include) instead.
   ProcessCommandsJson(content);

   // Clear file after processing so commands aren't re-run
   int wHandle = FileOpen(CommandFileName, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(wHandle != INVALID_HANDLE)
     {
      FileWriteString(wHandle, "[]");
      FileClose(wHandle);
     }
  }

//+------------------------------------------------------------------+
//| Very small JSON scanner: pulls out "action" and "params" per obj |
//+------------------------------------------------------------------+
void ProcessCommandsJson(string json)
  {
   int pos = 0;
   while(true)
     {
      int actionPos = StringFind(json, "\"action\"", pos);
      if(actionPos < 0)
         break;

      int colon = StringFind(json, ":", actionPos);
      int q1 = StringFind(json, "\"", colon + 1);
      int q2 = StringFind(json, "\"", q1 + 1);
      string action = StringSubstr(json, q1 + 1, q2 - q1 - 1);

      // look for optional "percent"
      int percent = 0;
      int pctPos = StringFind(json, "\"percent\"", q2);
      int nextAction = StringFind(json, "\"action\"", q2);
      if(pctPos >= 0 && (nextAction < 0 || pctPos < nextAction))
        {
         int pColon = StringFind(json, ":", pctPos);
         int pEnd = StringFind(json, ",", pColon);
         int pEnd2 = StringFind(json, "}", pColon);
         if(pEnd < 0 || (pEnd2 >= 0 && pEnd2 < pEnd))
            pEnd = pEnd2;
         string pctStr = StringSubstr(json, pColon + 1, pEnd - pColon - 1);
         StringTrimLeft(pctStr);
         StringTrimRight(pctStr);
         percent = (int)StringToInteger(pctStr);
        }

      ExecuteAction(action, percent);
      pos = q2 + 1;
     }
  }

//+------------------------------------------------------------------+
//| Dispatch one action                                               |
//+------------------------------------------------------------------+
void ExecuteAction(string action, int percent)
  {
   Print("Executing action: ", action, " percent=", percent);

   if(action == "SL_TO_BE")
     {
      MoveAllSlToBreakeven();
     }
   else if(action == "PARTIAL_CLOSE")
     {
      if(percent <= 0) percent = 50;
      PartialCloseAll(percent);
     }
   else if(action == "CLOSE_ALL")
     {
      CloseAllPositions();
     }
   else if(action == "DERISK")
     {
      MoveAllSlToBreakeven();
      PartialCloseAll(percent > 0 ? percent : 50);
     }
   else
     {
      Print("Unknown action received: ", action);
     }
  }

//+------------------------------------------------------------------+
//| Move SL to breakeven (entry price) for every open position       |
//+------------------------------------------------------------------+
void MoveAllSlToBreakeven()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket))
         continue;

      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double tp    = PositionGetDouble(POSITION_TP);
      string symbol = PositionGetString(POSITION_SYMBOL);

      MqlTradeRequest request;
      MqlTradeResult  result;
      ZeroMemory(request);
      ZeroMemory(result);

      request.action   = TRADE_ACTION_SLTP;
      request.position = ticket;
      request.symbol   = symbol;
      request.sl       = entry;
      request.tp       = tp;

      if(!OrderSend(request, result))
         Print("SL->BE failed for ticket ", ticket, " err=", GetLastError());
      else
         Print("SL moved to BE for ticket ", ticket);
     }
  }

//+------------------------------------------------------------------+
//| Partially close every open position by X percent of its volume   |
//+------------------------------------------------------------------+
void PartialCloseAll(int percent)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket))
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      double volume  = PositionGetDouble(POSITION_VOLUME);
      double minLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

      double closeVol = NormalizeVolume(volume * percent / 100.0, minLot, lotStep);
      if(closeVol < minLot)
        {
         Print("Skip partial close on ticket ", ticket, " — computed volume below min lot");
         continue;
        }
      // Don't try to close more than what's open
      if(closeVol >= volume)
         closeVol = volume;

      MqlTradeRequest request;
      MqlTradeResult  result;
      ZeroMemory(request);
      ZeroMemory(result);

      long type = PositionGetInteger(POSITION_TYPE);

      request.action   = TRADE_ACTION_DEAL;
      request.position = ticket;
      request.symbol   = symbol;
      request.volume   = closeVol;
      request.type     = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      request.price    = (type == POSITION_TYPE_BUY)
                            ? SymbolInfoDouble(symbol, SYMBOL_BID)
                            : SymbolInfoDouble(symbol, SYMBOL_ASK);
      request.deviation = 10;

      if(!OrderSend(request, result))
         Print("Partial close failed for ticket ", ticket, " err=", GetLastError());
      else
         Print("Partially closed ", closeVol, " lots on ticket ", ticket);
     }
  }

//+------------------------------------------------------------------+
//| Close every open position fully                                   |
//+------------------------------------------------------------------+
void CloseAllPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket))
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      double volume  = PositionGetDouble(POSITION_VOLUME);
      long type      = PositionGetInteger(POSITION_TYPE);

      MqlTradeRequest request;
      MqlTradeResult  result;
      ZeroMemory(request);
      ZeroMemory(result);

      request.action    = TRADE_ACTION_DEAL;
      request.position   = ticket;
      request.symbol     = symbol;
      request.volume     = volume;
      request.type       = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      request.price      = (type == POSITION_TYPE_BUY)
                              ? SymbolInfoDouble(symbol, SYMBOL_BID)
                              : SymbolInfoDouble(symbol, SYMBOL_ASK);
      request.deviation  = 10;

      if(!OrderSend(request, result))
         Print("Close failed for ticket ", ticket, " err=", GetLastError());
      else
         Print("Closed ticket ", ticket);
     }
  }

//+------------------------------------------------------------------+
//| Round a lot size down to the nearest valid step                  |
//+------------------------------------------------------------------+
double NormalizeVolume(double vol, double minLot, double lotStep)
  {
   if(lotStep <= 0) lotStep = 0.01;
   double steps = MathFloor(vol / lotStep);
   double normalized = steps * lotStep;
   if(normalized < minLot)
      normalized = 0;
   return NormalizeDouble(normalized, 2);
  }
//+------------------------------------------------------------------+
