import sys
import os
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np

# Mock MT5 before importing other modules
sys.modules['MetaTrader5'] = MagicMock()
import MetaTrader5 as mt5

import config
from core.trade_manager import manage_open_trade
from ml.model import TradeLogger

class TestBotFixes(unittest.TestCase):
    def setUp(self):
        # Reset config to known state
        config.PROGRESSIVE_TRAIL = True
        config.TRAIL_STAGE_1_PROGRESS = 0.5
        config.TRAIL_STAGE_2_PROGRESS = 0.7
        config.TRAIL_STAGE_3_PROGRESS = 0.9
        config.TRAIL_STAGE_1_ATR_MULT = 2.5
        config.TRAIL_STAGE_2_ATR_MULT = 1.5
        config.TRAIL_STAGE_3_ATR_MULT = 0.8
        config.BREAK_EVEN_PROGRESS = 0.6
        
        # Mock TradeLogger path
        config.TRADE_LOG_PATH = "data/test_trade_log.csv"
        if os.path.exists(config.TRADE_LOG_PATH):
            os.remove(config.TRADE_LOG_PATH)

    def test_progressive_trailing_buy(self):
        """Test BUY progressive trailing stop stages"""
        symbol = "XAUUSDm"
        state = {
            'ticket': 1001,
            'entry': 2000.0,
            'tp': 2100.0,
            'sl': 1950.0,
            'direction': 1
        }
        
        # Mock MT5 position and tick
        mock_pos = MagicMock()
        mock_pos.ticket = 1001
        mock_pos.sl = 1950.0
        mock_pos.tp = 2100.0
        mt5.positions_get.return_value = [mock_pos]
        
        # ATR = 10
        df_feat = pd.DataFrame({'atr': [10.0]})
        
        # 1. Progress = 55% (Stage 1: 2.5x ATR)
        # Price = 2000 + (100 * 0.55) = 2055
        # Expected trail SL = 2055 - (10 * 2.5) = 2030
        mock_tick = MagicMock()
        mock_tick.bid = 2055.0
        mt5.symbol_info_tick.return_value = mock_tick
        
        with patch('core.trade_manager._modify_position') as mock_modify:
            mock_modify.return_value = True
            manage_open_trade(symbol, state, df_feat, MagicMock())
            
            # Verify stage 1 trail
            mock_modify.assert_called_with(1001, 2030.0, 2100.0)
            self.assertEqual(state['sl'], 2030.0)

    def test_trade_logger_ticket_matching(self):
        """Verify TradeLogger uses ticket for outcome matching"""
        logger = TradeLogger()
        symbol = "XAUUSDm"
        signal = {'bar_time': '2026-06-10', 'signal': 'BUY', 'atr': 10}
        
        # Log 3 positions for same symbol
        logger.log_trade(symbol, signal, 2000, 2100, 1950, 0.01, ticket=101)
        logger.log_trade(symbol, signal, 2000, 2100, 1950, 0.01, ticket=102)
        logger.log_trade(symbol, signal, 2000, 2100, 1950, 0.01, ticket=103)
        
        # Update only ticket 102
        logger.update_outcome(symbol, 102, 50.0)
        
        df = pd.read_csv(config.TRADE_LOG_PATH)
        self.assertEqual(len(df), 3)
        
        # Check that only ticket 102 is updated
        row101 = df[df['ticket'] == 101].iloc[0]
        row102 = df[df['ticket'] == 102].iloc[0]
        row103 = df[df['ticket'] == 103].iloc[0]
        
        self.assertTrue(np.isnan(row101['outcome']))
        self.assertEqual(row102['outcome'], 1)
        self.assertEqual(row102['profit'], 50.0)
        self.assertTrue(np.isnan(row103['outcome']))

if __name__ == '__main__':
    unittest.main()
