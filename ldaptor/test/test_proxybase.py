"""
Test cases for ldaptor.protocols.ldap.proxybase module.
"""
import itertools
from twisted.internet import error, defer
from twisted.internet.task import Clock, deferLater
from twisted.trial import unittest
from twisted.test import proto_helpers
from ldaptor import config
from ldaptor.protocols.ldap import proxybase, ldaperrors
from ldaptor.protocols import pureldap
from ldaptor import testutil


class RequestInterceptingProxy(proxybase.ProxyBase):
    """
    A test LDAP proxy that does not forward requests but instead
    responses with pre-determined responses.
    """
    responses = [pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode)]

    def handleBeforeForwardRequest(self, request, controls, reply):
        """
        Don't forward the message to the proxied service-- instead
        reply with predetermined responses.
        """
        for response in self.responses:
            reply(response)
        return defer.succeed(None)


class ResponseInterceptingProxy(proxybase.ProxyBase):
    """
    A test LDAP proxy that intercepts and modifies search results.
    """
    delays = itertools.cycle([1, 5])
    new_attrib = ('frotz', 'xyzzy')

    def handleProxiedResponse(self, response, request, controls):
        """
        If the response is an LDAPSearchResultEntry, modify
        the attribute list
        """
        if isinstance(response, pureldap.LDAPSearchResultEntry):
            key, value = self.new_attrib
            response.attributes.append((key, [value]))
        d = defer.Deferred()
        d.addCallback(self._afterDelay)
        self.reactor.callLater(self.delays.next(), d.callback, response)
        return d

    def _afterDelay(self, response):
        return response


class WontConnectError(Exception):
    pass


class WontConnectClientCreator(object):
    """
    A test LDAP client creator that will raise an exception when the
    `connect()` method is called.
    """
    ex = None
    delay = 0

    def __init__(self, *args, **kwgs):
        if self.ex is None:
            self.ex = WontConnectError("Test LDAP client refuses to connect.")
        self.reactor = kwgs.get('reactor', None)

    def connect(self, *args, **kwgs):
        delay = self.delay
        if delay == 0:
            return defer.fail(self.ex)
        else:
            d = deferLater(self.reactor, delay, self.refuseConnect_)
            return d

    def refuseConnect_(self):
        return defer.fail(self.ex)


def makeClientCreatorFactory(delay):
    def makeClientCreator(reactor_, protocol):
        cc = WontConnectClientCreator(reactor=reactor_)
        cc.delay = delay
        return cc
    return makeClientCreator


class ProxyBase(unittest.TestCase):
    def createServer(self, *responses, **kwds):
        """
        Create a server for each test.
        """
        protocol = kwds.get("protocol", proxybase.ProxyBase)
        clock = Clock()
        proto_args = dict(reactor_=clock)
        server = testutil.createServer(protocol, *responses, proto_args=proto_args)
        return server

    def test_bind(self):
        """
        When binding to the server an `LDAPBindResponse` with a successful
        result code.is written to the transport.
        """
        server = self.createServer([pureldap.LDAPBindResponse(resultCode=0)])
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPBindRequest(), id=4)))
        server.reactor.advance(1)
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=0), id=4)))

    def test_search(self):
        """
        When performing an LDAP search against the server; the search results and
        a single "search done" response is written to the transport.
        """
        server = self.createServer([pureldap.LDAPBindResponse(resultCode=0)],
                                   [pureldap.LDAPSearchResultEntry('cn=foo,dc=example,dc=com', [('a', ['b'])]),
                                    pureldap.LDAPSearchResultEntry('cn=bar,dc=example,dc=com', [('b', ['c'])]),
                                    pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode)],
                                   )
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPBindRequest(), id=2)))
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPSearchRequest(), id=3)))
        server.reactor.advance(1)
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=0), id=2))
                          + str(pureldap.LDAPMessage(pureldap.LDAPSearchResultEntry('cn=foo,dc=example,dc=com', [('a', ['b'])]), id=3))
                          + str(pureldap.LDAPMessage(pureldap.LDAPSearchResultEntry('cn=bar,dc=example,dc=com', [('b', ['c'])]), id=3))
                          + str(pureldap.LDAPMessage(pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode), id=3)))

    def test_unbind_clientUnbinds(self):
        """
        The server disconnects from the client gracefully when the
        client signals its intent to unbind.
        """
        server = self.createServer([pureldap.LDAPBindResponse(resultCode=0)], [],)
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPBindRequest(), id=2)))
        server.reactor.advance(1)
        client = server.client
        client.assertSent(pureldap.LDAPBindRequest())
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=0), id=2)))
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPUnbindRequest(), id=3)))
        server.connectionLost(error.ConnectionDone)
        server.reactor.advance(1)
        client.assertSent(pureldap.LDAPBindRequest(),
                          pureldap.LDAPUnbindRequest())
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=0), id=2)))

    def test_unbind_clientEOF(self):
        """
        The server disconects correctly when the client terminates the
        connection without sending an unbind request.
        """
        server = self.createServer([pureldap.LDAPBindResponse(resultCode=0)], [])
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPBindRequest(), id=2)))
        server.reactor.advance(1)
        client = server.client
        client.assertSent(pureldap.LDAPBindRequest())
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=0), id=2)))
        server.connectionLost(error.ConnectionDone)
        server.reactor.advance(1)
        client.assertSent(pureldap.LDAPBindRequest(),
                          'fake-unbind-by-LDAPClientTestDriver')
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=0), id=2)))

    def test_intercepted_search_request(self):
        """
        When performing an LDAP search against the server; the requests are
        intercepted and custom responses are written to the transport.
        """
        server = self.createServer(
            [pureldap.LDAPBindResponse(resultCode=0)],
            [
                pureldap.LDAPSearchResultEntry('cn=foo,dc=example,dc=com', [('a', ['b'])]),
                pureldap.LDAPSearchResultEntry('cn=bar,dc=example,dc=com', [('b', ['c'])]),
                pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode),
            ],
            protocol=RequestInterceptingProxy)
        server.responses = [
            pureldap.LDAPSearchResultEntry('cn=xyzzy,dc=example,dc=com', [('frobnitz', ['zork'])]),
            pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode)]
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPSearchRequest(), id=1)))
        server.reactor.advance(1)
        self.assertEquals(len(server.clientTestDriver.sent), 0)
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPSearchResultEntry('cn=xyzzy,dc=example,dc=com', [('frobnitz', ['zork'])]), id=1))
                          + str(pureldap.LDAPMessage(pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode), id=1)))

    def test_intercepted_search_response(self):
        """
        When performing an LDAP search against the server; the search results are
        intercepted and modified by the proxy.
        """
        server = self.createServer([pureldap.LDAPBindResponse(resultCode=0)],
                                   [pureldap.LDAPSearchResultEntry('cn=foo,dc=example,dc=com', [('a', ['b'])]),
                                    pureldap.LDAPSearchResultEntry('cn=bar,dc=example,dc=com', [('b', ['c'])]),
                                    pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode)],
                                   protocol=ResponseInterceptingProxy)
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPBindRequest(), id=2)))
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPSearchRequest(), id=3)))
        server.reactor.advance(1)
        server.reactor.advance(5)
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=0), id=2))
                          + str(pureldap.LDAPMessage(pureldap.LDAPSearchResultEntry('cn=foo,dc=example,dc=com', [('a', ['b']), ('frotz', ['xyzzy'])]), id=3))
                          + str(pureldap.LDAPMessage(pureldap.LDAPSearchResultEntry('cn=bar,dc=example,dc=com', [('b', ['c']), ('frotz', ['xyzzy'])]), id=3))
                          + str(pureldap.LDAPMessage(pureldap.LDAPSearchResultDone(ldaperrors.Success.resultCode), id=3)))

    def test_cannot_connect_to_proxied_server_no_pending_requests(self):
        """
        When making a request and the proxy cannot connect to the proxied server, the
        connection is terminated.
        """
        conf = config.LDAPConfig(serviceLocationOverrides={'': ('localhost', 8080)})
        server = proxybase.ProxyBase(conf, reactor_=Clock())
        server.transport = proto_helpers.StringTransport()
        server.clientCreator = makeClientCreatorFactory(0)
        server.connectionMade()
        server.clientCreator = WontConnectClientCreator
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPBindRequest(), id=4)))
        server.reactor.advance(1)
        self.assertEquals(server.transport.value(), "")

    def test_cannot_connect_to_proxied_server_pending_requests(self):
        """
        When making a request and the proxy cannot connect to the proxied server,
        pending BIND and startTLS requests are replied to and the connection
        is closed.
        """
        conf = config.LDAPConfig(serviceLocationOverrides={'': ('localhost', 8080)})
        server = proxybase.ProxyBase(conf, reactor_=Clock())
        server.transport = proto_helpers.StringTransport()
        server.clientCreator = makeClientCreatorFactory(2)
        server.connectionMade()
        server.dataReceived(str(pureldap.LDAPMessage(pureldap.LDAPBindRequest(), id=4)))
        server.reactor.advance(2)
        self.assertEquals(server.transport.value(),
                          str(pureldap.LDAPMessage(pureldap.LDAPBindResponse(resultCode=52), id=4)))
