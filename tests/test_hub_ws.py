import asyncio
import unittest


class TestHubWsNotify(unittest.TestCase):
    def _hub(self):
        from fxcm_api.data.hub import MarketHub
        hub = MarketHub.__new__(MarketHub)
        hub._ws_subscribers = set()
        return hub

    def test_subscribe_and_notify_wakes_waiter(self):
        hub = self._hub()

        async def scenario():
            evt = hub.subscribe_ws()
            waiter = asyncio.create_task(evt.wait())
            await asyncio.sleep(0.01)          # 让 waiter 挂起
            self.assertFalse(waiter.done())
            hub._notify_ws()                   # 模拟 tick 线程回调
            await asyncio.wait_for(waiter, timeout=1.0)
            evt.clear()
            hub.unsubscribe_ws(evt)
            self.assertEqual(len(hub._ws_subscribers), 0)
            # 退订后通知不再唤醒（无异常即可）
            hub._notify_ws()

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    def test_notify_with_closed_loop_discards(self):
        hub = self._hub()
        hub._ws_subscribers.add((None, asyncio.Event()))   # loop=None 模拟已关闭
        hub._notify_ws()                                   # RuntimeError → 清理
        self.assertEqual(len(hub._ws_subscribers), 0)


if __name__ == "__main__":
    unittest.main()
