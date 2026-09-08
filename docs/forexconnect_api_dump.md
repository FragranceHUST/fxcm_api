# forexconnect API 内省快照

- 包路径: `/Users/naxiong/Code/metatrader_dev/.venv/lib/python3.10/site-packages/forexconnect/__init__.py`
- 生成方式: `python scripts/dump_api_reference.py`（升级包后重跑刷新）

## ForexConnect 封装类方法

### ForexConnect.ACCOUNTS

```
    The enum specifies a set of values representing the trading tables.
```

### ForexConnect.CLOSED_TRADES

```
    The enum specifies a set of values representing the trading tables.
```

### ForexConnect.MESSAGES

```
    The enum specifies a set of values representing the trading tables.
```

### ForexConnect.OFFERS

```
    The enum specifies a set of values representing the trading tables.
```

### ForexConnect.ORDERS

```
    The enum specifies a set of values representing the trading tables.
```

### ForexConnect.ResponseType

```

```

### ForexConnect.SUMMARY

```
    The enum specifies a set of values representing the trading tables.
```

### ForexConnect.TRADES

```
    The enum specifies a set of values representing the trading tables.
```

### ForexConnect.TableManagerStatus

```

```

### ForexConnect.TableUpdateType

```

```

### ForexConnect.create_order_request

```
    Creates a request for creating an order of a specified type using specified parameters.
                Parameters
                ----------
                order_type : str
                    The type of the order. See Contansts.Orders.
                command : Commands
```

### ForexConnect.create_reader

```
    Creates a reader for a certain response using the method O2GResponseReaderFactory.create_reader.
                Parameters
                ----------
                response : O2GResponse
                    An instance of O2GResponse.
                Returns
```

### ForexConnect.create_request

```
    Creates a request.
                Parameters
                ----------
                params : typing.Dict[O2GRequestParamsEnum, str]
                    The request parameters. See O2GRequestParamsEnum.
                request_factory : O2GRequestFactory
```

### ForexConnect.get_history

```
    Gets price history of a certain instrument with a certain timeframe for a specified period or a certain number of bars/ticks.
                Parameters
                ----------
                instrument : str
                    The symbol of the instrument. The instrument must be one of the instruments the ForexConnect session is subscribed to.
                timeframe : str
```

### ForexConnect.get_table

```
    Gets a specified table.
                Parameters
                ----------
                table_type : O2GTableType
                    The identifier of the table. For a complete list of tables, see O2GTableType.
                Returns
```

### ForexConnect.get_table_reader

```
    Gets an instance of a table reader.
                Parameters
                ----------
                table_type : O2GTableType
                    The identifier of the table (see O2GTableType).
                response : O2GResponse
```

### ForexConnect.get_timeframe

```
    Gets an instance of a timeframe.
                Parameters
                ----------
                str_timeframe : str
                    The unique identifier of the timeframe.
                Returns
```

### ForexConnect.login

```
    Creates a trading session and starts the connection with the specified trade server.
                Parameters
                ----------
                user_id : str
                    The user name.
                password : str
```

### ForexConnect.login_rules

```
    Gets the rules used for the currently established session.
                Returns
                -------
                O2GLoginRules
```

### ForexConnect.login_with_token

```
    Creates a second trading session and starts the connection with the specified trade server using a token.
                Parameters
                ----------
                user_id : str
                    The user name.
                token : str
```

### ForexConnect.logout

```

```

### ForexConnect.parse_timeframe

```
    Parses a timeframe into O2GTimeframeUnit and the number of units.
                Parameters
                ----------
                timeframe : str
                    The unique identifier of the timeframe.
                Returns
```

### ForexConnect.response_listener

```
    Reserved for future use.
```

### ForexConnect.send_request

```
    Sends a request and returns the appropriate response reader or a bool value.
                Parameters
                ----------
                request : O2GRequest
                    An instance of O2GRequest.
                listener : ResponseListener
```

### ForexConnect.send_request_async

```
    Sends a request.
                Parameters
                ----------
                request : O2GRequest
                    An instance of O2GRequest.
                listener : ResponseListener
```

### ForexConnect.session

```
    Gets an instance of the current session.
                Returns
                -------
                O2GSession
```

### ForexConnect.set_session_status_listener

```
    Sets a session status listener on login or sets a new session status listener when necessary.
                Parameters
                ----------
                listener : typing.Callable[[O2GSession, AO2GSessionStatus.O2GSessionStatus], None]
                    The function that is called when the session status changes.
                Returns
```

### ForexConnect.subscribe_response

```
    Reserved for future use.
```

### ForexConnect.table_manager

```
    Gets the current table manager of the session.
                Returns
                -------
                O2GTableManager
```

### ForexConnect.unsubscribe_response

```
    Reserved for future use.
```

## fxcorepy 类清单

`AO2GAllEventQueueListener`，`AO2GChartSessionStatus`，`AO2GCommissionProviderListener`，`AO2GEachRowListener`，`AO2GResponseListener`，`AO2GRolloverProviderListener`，`AO2GSessionStatus`，`AO2GSystemPropertiesListener`，`AO2GTableListener`，`AO2GTableManagerListener`，`AO2GUpdateEventQueueListener`，`AO2GUpdatesProcessStatusListener`，`APriceHistoryCommunicatorListener`，`APriceHistoryCommunicatorStatusListener`，`Constants`，`O2GAccountRow`，`O2GAccountTableRow`，`O2GAllEventQueue`，`O2GAllEventQueueItem`，`O2GCandleOpenPriceMode`，`O2GChartSessionMode`，`O2GClosedTradeRow`，`O2GClosedTradeTableRow`，`O2GCommissionDescription`，`O2GCommissionDescriptionsCollection`，`O2GCommissionStage`，`O2GCommissionStatus`，`O2GCommissionUnitType`，`O2GCommissionsProvider`，`O2GGenericTableResponseReader`，`O2GLastOrderUpdateResponseReader`，`O2GLevel2MarketDataUpdatesReader`，`O2GLevel2MarketDataUpdatesReaderL1`，`O2GLevel2MarketDataUpdatesReaderPrice`，`O2GLogicOperators`，`O2GLoginRules`，`O2GMargins`，`O2GMarketDataResponseReader`，`O2GMarketDataSnapshotResponseReader`，`O2GMarketDataSnapshotResponseReaderItem`，`O2GMarketStatus`，`O2GMessageRow`，`O2GMessageTableRow`，`O2GOfferRow`，`O2GOfferTableRow`，`O2GOrderResponseReader`，`O2GOrderRow`，`O2GOrderTableRow`，`O2GPermissionChecker`，`O2GPermissionStatus`，`O2GPriceUpdateMode`，`O2GRelationalOperators`，`O2GReportUrlError`，`O2GRequest`，`O2GRequestFactory`，`O2GRequestParamsEnum`，`O2GResponse`，`O2GResponseReaderFactory`，`O2GResponseType`，`O2GRolloverProvider`，`O2GRolloverStatus`，`O2GRow`，`O2GSession`，`O2GSessionDescriptor`，`O2GSessionDescriptorCollection`，`O2GSummaryRow`，`O2GSummaryTableRow`，`O2GSystemPropertiesReader`，`O2GSystemProperty`，`O2GTable`，`O2GTableColumn`，`O2GTableColumnCollection`，`O2GTableColumnType`，`O2GTableEventsFilter`，`O2GTableIterator`，`O2GTableManager`，`O2GTableManagerMode`，`O2GTableManagerStatus`，`O2GTableStatus`，`O2GTableType`，`O2GTableUpdateType`，`O2GTablesUpdatesReader`，`O2GTablesUpdatesReaderItem`，`O2GTimeConverter`，`O2GTimeFrame`，`O2GTimeFrameCollection`，`O2GTimeFrameUnit`，`O2GTokenError`，`O2GTradeRow`，`O2GTradeTableRow`，`O2GTradingSettingsProvider`，`O2GTransport`，`O2GUpdateEventQueue`，`O2GUpdatesProcessStatus`，`O2GUserKind`，`O2GValueMap`，`PriceHistoryCommunicator`，`PriceHistoryCommunicatorFactory`，`PriceHistoryCommunicatorRequest`，`PriceHistoryCommunicatorResponse`，`PriceHistoryError`，`PriceHistoryErrorCode`，`QuotesManagerError`，`QuotesManagerErrorCode`，`TimeframeFactory`

## 关键类详情

### O2GSession

成员: `__dict__`, `__weakref__`, `chart_session_mode`, `chart_session_status`, `commissions_provider`, `force_close`, `get_report_url`, `get_table_manager_by_account`, `login`, `login_rules`, `login_with_token`, `logout`, `max_price_refresh_rate`, `min_price_refresh_rate`, `price_refresh_rate`, `price_update_mode`, `request_factory`, `requests_timeout`, `response_reader_factory`, `rollover_provider`, `send_request`, `server_time`, `session_status`, `session_sub_id`, `set_trading_session`, `subscribe_chart_session_status`, `subscribe_response`, `subscribe_session_status`, `subscribe_system_properties_change`, `table_manager`, `time_converter`, `token`, `trading_session_descriptors`, `unsubscribe_chart_session_status`, `unsubscribe_response`, `unsubscribe_session_status`, `unsubscribe_system_properties_change`, `use_table_manager`, `user_kind`, `user_name`

    A session object.

### O2GLoginRules

成员: `__dict__`, `__weakref__`, `get_table_refresh_response`, `is_table_loaded_by_default`, `permission_checker`, `system_properties_response`, `trading_settings_provider`

    Information about the rules used during the login in the currently established session.

### O2GTradingSettingsProvider

成员: `__dict__`, `__weakref__`, `get_base_unit_size`, `get_cond_dist_entry_limit`, `get_cond_dist_entry_stop`, `get_cond_dist_limit_for_trade`, `get_cond_dist_stop_for_trade`, `get_margins`, `get_market_status`, `get_max_quantity`, `get_min_quantity`, `get_mmr`, `max_trailing_step`, `min_trailing_step`

    Checks trading settings.

### O2GTableManager

成员: `__dict__`, `__weakref__`, `get_table`, `lock_updates`, `status`, `subscribe_updates_process_status`, `tables_update_event_queue`, `unlock_updates`, `unsubscribe_updates_process_status`

    The class creates and maintains trading tables in the ForexConnect memory.

### O2GTable

成员: `__dict__`, `__getitem__`, `__iter__`, `__len__`, `__weakref__`, `all_event_queue`, `columns`, `for_each_row`, `get_row`, `get_rows_by_column_value`, `get_rows_by_column_values`, `get_rows_by_condition`, `get_rows_by_multi_column_values`, `get_update_event_queue`, `is_cell_changed`, `is_cell_valid`, `size`, `status`, `subscribe_status`, `subscribe_update`, `table_events_filter`, `type`, `unsubscribe_status`, `unsubscribe_update`

    The class provides access to a table.

### O2GTradeTableRow

成员: `__dict__`, `__getattr__`, `__getitem__`, `__weakref__`, `columns`, `get_cell`, `is_cell_changed`, `table_type`

    The class provides access to the open position information and calculated fields.

### O2GOfferTableRow

成员: `__dict__`, `__getattr__`, `__getitem__`, `__weakref__`, `columns`, `get_cell`, `is_ask_change_direction_valid`, `is_ask_expire_date_valid`, `is_ask_id_valid`, `is_ask_tradable_valid`, `is_ask_valid`, `is_bid_change_direction_valid`, `is_bid_expire_date_valid`, `is_bid_id_valid`, `is_bid_tradable_valid`, `is_bid_valid`, `is_buy_interest_valid`, `is_cell_changed`, `is_contract_currency_valid`, `is_contract_multiplier_valid`, `is_default_sort_order_valid`, `is_digits_valid`, `is_dividend_buy_valid`, `is_dividend_sell_valid`, `is_fractional_pip_size_valid`, `is_hi_change_direction_valid`, `is_high_valid`, `is_instrument_type_valid`, `is_instrument_valid`, `is_low_change_direction_valid`, `is_low_valid`, `is_offer_id_valid`, `is_point_size_valid`, `is_quote_id_valid`, `is_sell_interest_valid`, `is_subscription_status_valid`, `is_time_valid`, `is_trading_status_valid`, `is_value_date_valid`, `is_volume_valid`, `table_type`

    The class provides access to the offer information and calculated fields.

### O2GAccountTableRow

成员: `__dict__`, `__getattr__`, `__getitem__`, `__weakref__`, `columns`, `get_cell`, `is_cell_changed`, `table_type`

    The class provides access to the account information and calculated fields.

### O2GOrderTableRow

成员: `__dict__`, `__getattr__`, `__getitem__`, `__weakref__`, `columns`, `get_cell`, `is_cell_changed`, `table_type`

    The class provides access to the order information and calculated fields.

### O2GClosedTradeTableRow

成员: `__dict__`, `__getattr__`, `__getitem__`, `__weakref__`, `columns`, `get_cell`, `is_cell_changed`, `table_type`

    The class provides access to the closed position information.

### O2GSummaryTableRow

成员: `__dict__`, `__getattr__`, `__getitem__`, `__weakref__`, `columns`, `get_cell`, `is_cell_changed`, `table_type`

    The class provides access to the summary information of the instrument traded.

### O2GMessageTableRow

成员: `__dict__`, `__getattr__`, `__getitem__`, `__weakref__`, `columns`, `get_cell`, `is_cell_changed`, `table_type`

    The class provides access to the message information.

### O2GRequest

成员: `__dict__`, `__getitem__`, `__len__`, `__weakref__`, `children_count`, `get_child_request`, `request_id`, `size`

    A request to the server.

### O2GRequestFactory

成员: `__dict__`, `__weakref__`, `create_confirmation_mail_request`, `create_market_data_snapshot_request_instrument`, `create_order_request`, `create_refresh_table_request`, `create_refresh_table_request_by_account`, `create_value_map`, `fill_market_data_snapshot_request_time`, `last_error`, `timeframe_collection`

    A request factory.

### O2GValueMap

成员: `__dict__`, `__len__`, `__weakref__`, `append_child`, `children_count`, `clear`, `clone`, `get_child`, `set_boolean`, `set_double`, `set_int`, `set_string`

    A value map containing order parameters.

### O2GTableColumn

成员: `__dict__`, `__weakref__`, `id`, `is_key`, `type`

    The class provides access to a trading table column.

### PriceHistoryCommunicator

成员: `__dict__`, `__weakref__`, `add_listener`, `add_status_listener`, `cancel_request`, `candle_open_price_mode`, `create_request`, `create_response_reader`, `get_history`, `is_ready`, `remove_listener`, `remove_status_listener`, `send_request`, `timeframe_collection`, `timeframe_factory`

    Reserved for future use.

### O2GSessionDescriptor

成员: `__dict__`, `__weakref__`, `description`, `id`, `name`, `requires_pin`

    A trading session descriptor.

## Constants 枚举值

- **BUY** = `'B'`
### Constants.Commands

- `Commands.ACCEPT_ORDER` = `'AcceptOrder'`
- `Commands.CHANGE_PASSWORD` = `'ChangePassword'`
- `Commands.CREATE_OCO` = `'CreateOCO'`
- `Commands.CREATE_ORDER` = `'CreateOrder'`
- `Commands.CREATE_OTO` = `'CreateOTO'`
- `Commands.CREATE_OTOCO` = `'CreateOTOCO'`
- `Commands.DELETE_ORDER` = `'DeleteOrder'`
- `Commands.EDIT_ORDER` = `'EditOrder'`
- `Commands.GET_LAST_ORDER_UPDATE` = `'GetLastOrderUpdate'`
- `Commands.JOIN_TO_EXISTING_CONTINGENCY_GROUP` = `'JoinToExistingContingencyGroup'`
- `Commands.JOIN_TO_NEW_CONTINGENCY_GROUP` = `'JoinToNewContingencyGroup'`
- `Commands.REMOVE_FROM_CONTINGENCY_GROUP` = `'RemoveFromContingencyGroup'`
- `Commands.SEND_MAIL` = `'SendMail'`
- `Commands.SET_SUBSCRIPTION_STATUS` = `'SetSubscriptionStatus'`
- `Commands.UPDATE_COMMISSIONS` = `'UpdateCommissions'`
- `Commands.UPDATE_MARGIN_REQUIREMENTS` = `'UpdateMarginRequirements'`
- `Commands.UPDATE_ROLLOVER` = `'UpdateRollover'`

### Constants.KeyType

- `KeyType.ORDER_ID` = `'OrderID'`
- `KeyType.REQUEST_ID` = `'OrderQID'`
- `KeyType.REQUEST_TXT` = `'OrderQTXT'`

### Constants.MessageFeature

- `MessageFeature.EMERGENCY` = `'7'`
- `MessageFeature.INFORMATION` = `'4'`
- `MessageFeature.MARKET_CONDITION` = `'5'`
- `MessageFeature.PLAIN` = `'1'`
- `MessageFeature.QUESTION` = `'3'`
- `MessageFeature.SOFTWARE_UPDATE` = `'6'`
- `MessageFeature.SYSTEM_FAILURE` = `'8'`
- `MessageFeature.TRADING_HOURS` = `'2'`

### Constants.MessageType

- `MessageType.ANSWER` = `'2'`
- `MessageType.FORCED_POPUP` = `'3'`
- `MessageType.POPUP` = `'1'`
- `MessageType.REGULAR` = `'0'`

### Constants.Orders

- `Orders.CLOSE_LIMIT` = `'CL'`
- `Orders.ENTRY` = `'E'`
- `Orders.LIMIT` = `'L'`
- `Orders.LIMIT_ENTRY` = `'LE'`
- `Orders.LIMIT_TRAILING_ENTRY` = `'LTE'`
- `Orders.MARKET_CLOSE` = `'C'`
- `Orders.MARKET_CLOSE_RANGE` = `'CR'`
- `Orders.MARKET_OPEN` = `'O'`
- `Orders.MARKET_OPEN_RANGE` = `'OR'`
- `Orders.OPEN_LIMIT` = `'OL'`
- `Orders.RANGE_ENTRY` = `'RE'`
- `Orders.RANGE_TRAILING_ENTRY` = `'RTE'`
- `Orders.STOP` = `'S'`
- `Orders.STOP_ENTRY` = `'SE'`
- `Orders.STOP_TRAILING_ENTRY` = `'STE'`
- `Orders.TRUE_MARKET_CLOSE` = `'CM'`
- `Orders.TRUE_MARKET_OPEN` = `'OM'`

### Constants.Peg

- `Peg.FROM_CLOSE` = `'M'`
- `Peg.FROM_OPEN` = `'O'`

- **SELL** = `'S'`
### Constants.SubscriptionStatuses

- `SubscriptionStatuses.DISABLE` = `'D'`
- `SubscriptionStatuses.TRADABLE` = `'T'`
- `SubscriptionStatuses.VIEW_ONLY` = `'V'`

### Constants.SystemProperties

- `SystemProperties.BASE_CRNCY` = `'BASE_CRNCY'`
- `SystemProperties.BASE_CRNCY_PRECISION` = `'BASE_CRNCY_PRECISION'`
- `SystemProperties.BASE_CRNCY_SYMBOL` = `'BASE_CRNCY_SYMBOL'`
- `SystemProperties.BASE_TIME_ZONE` = `'BASE_TIME_ZONE'`
- `SystemProperties.BASE_UNIT_SIZE` = `'BASE_UNIT_SIZE'`
- `SystemProperties.COND_DIST` = `'COND_DIST'`
- `SystemProperties.COND_DIST_ENTRY` = `'COND_DIST_ENTRY'`
- `SystemProperties.CP_170` = `'CP_170'`
- `SystemProperties.CP_171` = `'CP_171'`
- `SystemProperties.CP_172` = `'CP_172'`
- `SystemProperties.CP_86` = `'CP_86'`
- `SystemProperties.CP_88` = `'CP_88'`
- `SystemProperties.CP_89` = `'CP_89'`
- `SystemProperties.CP_94` = `'CP_94'`
- `SystemProperties.END_TRADING_DAY` = `'END_TRADING_DAY'`
- `SystemProperties.FIRST_TICK_OPEN_PRICE_ENABLED` = `'FIRST_TICK_OPEN_PRICE_ENABLED'`
- `SystemProperties.FORCE_PASSWORD_CHANGE` = `'FORCE_PASSWORD_CHANGE'`
- `SystemProperties.MARKET_OPEN` = `'MARKET_OPEN'`
- `SystemProperties.PEGGED_STOP_LIMIT_DISABLED` = `'PEGGED_STOP_LIMIT_DISABLED'`
- `SystemProperties.QUERYDEPTH_0` = `'QUERYDEPTH_0'`
- `SystemProperties.QUERYDEPTH_1` = `'QUERYDEPTH_1'`
- `SystemProperties.QUERYDEPTH_2` = `'QUERYDEPTH_2'`
- `SystemProperties.QUERYDEPTH_3` = `'QUERYDEPTH_3'`
- `SystemProperties.QUERYDEPTH_4` = `'QUERYDEPTH_4'`
- `SystemProperties.QUERYDEPTH_5` = `'QUERYDEPTH_5'`
- `SystemProperties.QUERYDEPTH_6` = `'QUERYDEPTH_6'`
- `SystemProperties.QUERYDEPTH_7` = `'QUERYDEPTH_7'`
- `SystemProperties.QUERYDEPTH_8` = `'QUERYDEPTH_8'`
- `SystemProperties.QUERYDEPTH_h2` = `'QUERYDEPTH_h2'`
- `SystemProperties.QUERYDEPTH_h3` = `'QUERYDEPTH_h3'`
- `SystemProperties.QUERYDEPTH_h4` = `'QUERYDEPTH_h4'`
- `SystemProperties.QUERYDEPTH_h6` = `'QUERYDEPTH_h6'`
- `SystemProperties.QUERYDEPTH_h8` = `'QUERYDEPTH_h8'`
- `SystemProperties.SERVER_TIME_UTC` = `'SERVER_TIME_UTC'`
- `SystemProperties.SUPPORT_TICK_VOLUME` = `'SupportTickVolume'`
- `SystemProperties.TP_170` = `'TP_170'`
- `SystemProperties.TP_171` = `'TP_171'`
- `SystemProperties.TP_172` = `'TP_172'`
- `SystemProperties.TP_86` = `'TP_86'`
- `SystemProperties.TP_88` = `'TP_88'`
- `SystemProperties.TP_89` = `'TP_89'`
- `SystemProperties.TP_94` = `'TP_94'`
- `SystemProperties.TRAILING_DYNAMIC` = `'TRAILING_DYNAMIC'`
- `SystemProperties.TRAILING_FLUCTUATE` = `'TRAILING_FLUCTUATE'`
- `SystemProperties.TRAILING_FLUCTUATE_PTS_MAX` = `'TRAILING_FLUCTUATE_PTS_MAX'`
- `SystemProperties.TRAILING_FLUCTUATE_PTS_MIN` = `'TRAILING_FLUCTUATE_PTS_MIN'`

### Constants.TIF

- `TIF.DAY` = `'DAY'`
- `TIF.FOK` = `'FOK'`
- `TIF.GTC` = `'GTC'`
- `TIF.GTD` = `'GTD'`
- `TIF.IOC` = `'IOC'`

## O2GRequestParamsEnum（create_order_request 的 kwargs 全集）

- `ACCOUNT_ID`
- `ACCOUNT_NAME`
- `ACCT_ID`
- `AMOUNT`
- `ASK`
- `AUTO_LIMIT`
- `AUTO_MRGN`
- `BID`
- `BUY_INTR`
- `BUY_SELL`
- `CLIENT_RATE`
- `COMMAND`
- `COND_DISTANCE`
- `COND_DISTANCE_E`
- `CONTINGENCY_GROUP_TYPE`
- `CONTINGENCY_ID`
- `CROSS_CURRENCY`
- `CUSTOM_ID`
- `DEALER_INT_FLG`
- `ENTRY_MRGN_REQ`
- `EQTY_ENABLED_FLG`
- `EQTY_LIMIT`
- `EQTY_STOP`
- `EXPIRE_DATE_TIME`
- `FEED`
- `FEED_ASK`
- `FEED_BID`
- `FEED_PRICE`
- `GONE_TO_PEE_FLG`
- `ID`
- `INTR_BUY`
- `INTR_FLAG`
- `INTR_MULT`
- `INTR_MULT_NONE`
- `INTR_SEL`
- `INTR_SIGN`
- `KEY`
- `LIFETIME`
- `LOGIN`
- `LOGIN_ID`
- `MANUAL_PRICES`
- `MAX_QUANTITY`
- `MRGN_ENABLED_FLG`
- `MRGN_REQ`
- `MRGN_REQ_AWARE`
- `MRGN_REQ_ENTRY`
- `MSG`
- `MSG_DELETE`
- `MSG_DELIVER`
- `MSG_FEATURE`
- `MSG_ID`
- `MSG_SUBJECT`
- `MSG_TEXT`
- `MSG_TO`
- `MSG_TYPE`
- `NET_QUANTITY`
- `OFFER_ID`
- `ORDER_ID`
- `ORDER_PRICE`
- `ORDER_PRICE_FLG`
- `ORDER_TYPE`
- `ORDR_LIFETIME`
- `PANIC_FLG`
- `PANIC_LEVEL`
- `PEG_OFFSET`
- `PEG_OFFSET_LIMIT`
- `PEG_OFFSET_MAX`
- `PEG_OFFSET_MIN`
- `PEG_OFFSET_STOP`
- `PEG_TYPE`
- `PEG_TYPE_LIMIT`
- `PEG_TYPE_STOP`
- `PERCENT_COST`
- `PRIMARY_QID`
- `PSW`
- `RATE`
- `RATE_LIMIT`
- `RATE_MAX`
- `RATE_MIN`
- `RATE_STOP`
- `RATE_VARIAT`
- `REPORT_ID`
- `RFQ_LIFETIME`
- `SEAT_BELT`
- `SELL_INTR`
- `STATUS`
- `SUBSCRIPTION_STATUS`
- `SYMBOL`
- `TIME_IN_FORCE`
- `TRADE_ID`
- `TRAIL_STEP`
- `TRAIL_STEP_STOP`
- `UNKNOWN_PARAM`

## O2GTableType

- `ACCOUNTS` = `forexconnect.lib.fxcorepy.O2GTableType.ACCOUNTS`
- `CLOSED_TRADES` = `forexconnect.lib.fxcorepy.O2GTableType.CLOSED_TRADES`
- `MESSAGES` = `forexconnect.lib.fxcorepy.O2GTableType.MESSAGES`
- `OFFERS` = `forexconnect.lib.fxcorepy.O2GTableType.OFFERS`
- `ORDERS` = `forexconnect.lib.fxcorepy.O2GTableType.ORDERS`
- `SUMMARY` = `forexconnect.lib.fxcorepy.O2GTableType.SUMMARY`
- `TABLE_UNKNOWN` = `forexconnect.lib.fxcorepy.O2GTableType.TABLE_UNKNOWN`
- `TRADES` = `forexconnect.lib.fxcorepy.O2GTableType.TRADES`
- `as_integer_ratio` = `<method 'as_integer_ratio' of 'int' objects>`
- `bit_count` = `<method 'bit_count' of 'int' objects>`
- `bit_length` = `<method 'bit_length' of 'int' objects>`
- `conjugate` = `<method 'conjugate' of 'int' objects>`
- `denominator` = `<attribute 'denominator' of 'int' objects>`
- `from_bytes` = `<built-in method from_bytes of type object at 0xb38dc1010>`
- `imag` = `<attribute 'imag' of 'int' objects>`
- `name` = `<member 'name' of 'Boost.Python.enum' objects>`
- `names` = `{'TABLE_UNKNOWN': forexconnect.lib.fxcorepy.O2GTableType.TABLE_UNKNOWN, 'OFFERS': forexconnect.lib.fxcorepy.O2GTableType.OFFERS, 'ACCOUNTS': forexconnect.lib.fxcorepy.O2GTableType.ACCOUNTS, 'ORDERS': forexconnect.lib.fxcorepy.O2GTableType.ORDERS, 'TRADES': forexconnect.lib.fxcorepy.O2GTableType.TRADES, 'CLOSED_TRADES': forexconnect.lib.fxcorepy.O2GTableType.CLOSED_TRADES, 'MESSAGES': forexconnect.lib.fxcorepy.O2GTableType.MESSAGES, 'SUMMARY': forexconnect.lib.fxcorepy.O2GTableType.SUMMARY}`
- `numerator` = `<attribute 'numerator' of 'int' objects>`
- `real` = `<attribute 'real' of 'int' objects>`
- `to_bytes` = `<method 'to_bytes' of 'int' objects>`
- `values` = `{-1: forexconnect.lib.fxcorepy.O2GTableType.TABLE_UNKNOWN, 0: forexconnect.lib.fxcorepy.O2GTableType.OFFERS, 1: forexconnect.lib.fxcorepy.O2GTableType.ACCOUNTS, 2: forexconnect.lib.fxcorepy.O2GTableType.ORDERS, 3: forexconnect.lib.fxcorepy.O2GTableType.TRADES, 4: forexconnect.lib.fxcorepy.O2GTableType.CLOSED_TRADES, 5: forexconnect.lib.fxcorepy.O2GTableType.MESSAGES, 6: forexconnect.lib.fxcorepy.O2GTableType.SUMMARY}`
