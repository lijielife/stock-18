# encoding: utf-8
from __future__ import unicode_literals

from scrapy import signals
from scrapy.signalmanager import SignalManager
from scrapy.responsetypes import responsetypes
from scrapy.xlib.pydispatch import dispatcher
from selenium import webdriver
import Queue
from twisted.internet import defer, threads
from twisted.python.failure import Failure
from scrapy import log
from stock.http import PhantomJSRequest
from scrapy.utils.misc import load_object


FALLBACK_HANDLER = 'scrapy.core.downloader.handlers.http.HTTPDownloadHandler'

class PhantomJSDownloadHandler(object):

    def __init__(self, settings):
        self.options = settings.get('PHANTOMJS_OPTIONS', {})
        max_run = settings.get('PHANTOMJS_MAXRUN', 10)
        self.sem = defer.DeferredSemaphore(max_run)
        self.queue = Queue.LifoQueue(max_run)
        self.create_phantomjs_count = 0
        self._fallback_handler = load_object(FALLBACK_HANDLER)(settings)
        SignalManager(dispatcher.Any).connect(self._close, signal=signals.spider_closed)

    def download_request(self, request, spider):
        """use semaphore to guard a phantomjs pool"""
        if isinstance(request, PhantomJSRequest):
            return self.sem.run(self._wait_request, request, spider)
        else:
            return self._fallback_handler.download_request(request, spider)

    def _wait_request(self, request, spider):
        try:
            driver = self.queue.get_nowait()
        except Queue.Empty:
            driver = webdriver.PhantomJS(**self.options)
            self.create_phantomjs_count += 1
        log.msg('PhantomJS Requesting: %s' % request.url, level=log.INFO)
        driver.get(request.url)
        # ghostdriver won't response when switch window until page is loaded
        dfd = threads.deferToThread(lambda: driver.switch_to.window(driver.current_window_handle))
        dfd.addCallback(self._response, driver, spider)
        return dfd

    def _response(self, _, driver, spider):
        body = driver.execute_script("return document.documentElement.innerHTML")
        if body.startswith("<head></head>"):  # cannot access response header in Selenium
            body = driver.execute_script("return document.documentElement.textContent")
        url = driver.current_url
        respcls = responsetypes.from_args(url=url, body=body[:100].encode('utf8'))
        resp = respcls(url=url, body=body, encoding="utf-8")

        response_failed = getattr(spider, "response_failed", None)
        if response_failed and callable(response_failed) and response_failed(resp, driver):
            driver.close()
            return defer.fail(Failure())
        else:
            self.queue.put(driver)
            return defer.succeed(resp)

    def _close(self):
        log.msg('create PhantomJS instance count: %s' % self.create_phantomjs_count, level=log.INFO)
        while not self.queue.empty():
            driver = self.queue.get_nowait()
            driver.close()


