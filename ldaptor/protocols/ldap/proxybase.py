"""
LDAP protocol proxy server.
"""
from __future__ import absolute_import, division, print_function
from ldaptor import config
from ldaptor.protocols.ldap import ldapserver, ldapconnector, ldapclient, ldaperrors
from ldaptor.protocols import pureldap
from twisted.internet import reactor, defer
from twisted.python import log

class ProxyBase(ldapserver.BaseLDAPServer):
    """
    An LDAP server proxy.
    Override `handleBeforeForwardRequest()` to inspect/modify requests from
    the client.
    Override `handleBeforeForwardRequest()` to inspect/modify responses from
    the proxied server.
    """
    protocol = ldapclient.LDAPClient
    client = None
    unbound = False
    clientCreator = ldapconnector.LDAPClientCreator

    def __init__(self, config, use_tls=False, reactor_=reactor):
        """
        Initialize the object.

        :param config: The configuration.
        :type config: ldaptor.interfaces.ILDAPConfig

        :param use_tls: Force connections to the proxied server to use startTLS.
        :type use_tls: boolean

        :param reactor_: Use a reactor other than the default one.
        """
        ldapserver.BaseLDAPServer.__init__(self)
        self.config = config
        # Requests that are ready before the client connection is established
        # are queued.
        self.queuedRequests = []
        self.use_tls = use_tls
        self.reactor = reactor_
        self.startTLS_initiated = False

    def connectionMade(self):
        """
        Establish a connection to the proxied LDAP server.
        """
        clientCreator = self.clientCreator(self.reactor, self.protocol)
        d = clientCreator.connect(
            dn='',
            overrides=self.config.getServiceLocationOverrides())
        d.addCallback(self._connectedToProxiedServer)
        d.addErrback(self._failedToConnectToProxiedServer)
        ldapserver.BaseLDAPServer.connectionMade(self)

    def connectionLost(self, reason):
        if self.client is not None and self.client.connected:
            if not self.unbound:
                self.client.unbind()
                self.unbound = True
            else:
                self.client.transport.loseConnection()
        self.client = None
        ldapserver.BaseLDAPServer.connectionLost(self, reason)

    def _connectedToProxiedServer(self, proto):
        """
        The connection to the proxied server is set up.
        """
        if self.use_tls:
            d = proto.startTLS()
            d.addCallback(self._establishedTLS)
            return d
        else:
            self.client = proto
            self._processBacklog()

    def _establishedTLS(self, proto):
        """
        TLS has been started.
        Process any backlog of requests.
        """
        self.client = proto
        self._processBacklog()

    def _failedToConnectToProxiedServer(self, err):
        """
        The connection to the proxied server failed.
        """
        log.msg("[ERROR] Could not connect to proxied server.  Error was:\n{0}".format(err))
        while len(self.queuedRequests) > 0:
            request, controls, reply = self.queuedRequests.pop(0)
            if isinstance(request, pureldap.LDAPBindRequest):
                msg = pureldap.LDAPBindResponse(resultCode=ldaperrors.LDAPUnavailable.resultCode)
            elif isinstance(request, pureldap.LDAPStartTLSRequest):
                msg = pureldap.LDAPStartTLSResponse(resultCode=ldaperrors.LDAPUnavailable.resultCode)
            else:
                continue
            reply(msg)
        self.transport.loseConnection()

    def _processBacklog(self):
        """
        Process the backlog of requests.
        """
        while len(self.queuedRequests) > 0:
            request, controls, reply = self.queuedRequests.pop(0)
            self._forwardRequestToProxiedServer(request, controls, reply)

    def _forwardRequestToProxiedServer(self, request, controls, reply):
        """
        Forward the original requests to the proxied server.
        """
        if self.client is None:
            self.queuedRequests.append((request, controls, reply))
            return
        def forwardit(result, reply):
            """
            Forward the LDAP request to the proxied server.
            """
            if result is None:
                return
            request, controls = result
            if request.needs_answer:
                dseq = []
                d2 = self.client.send_multiResponse(request, self._gotResponseFromProxiedServer, reply, request, controls, dseq)
                d2.addErrback(log.err)
            else:
                self.client.send_noResponse(request)
        d = defer.maybeDeferred(self.handleBeforeForwardRequest, request, controls, reply)
        d.addCallback(forwardit, reply)

    def handleBeforeForwardRequest(self, request, controls, reply):
        """
        Override to modify request and/or controls forwarded on to the proxied server.
        Must return a tuple of request, controls or a deferred that fires the same.
        Return `None` or a deferred that fires `None` to bypass forwarding the 
        request to the proxied server.  In this case, any response can be sent to the
        client via `reply(response)`.
        """
        return defer.succeed((request, controls))

    def _gotResponseFromProxiedServer(self, response, reply, request, controls, dseq):
        """
        Returns True if this is the last response to the request.
        """
        d = defer.maybeDeferred(self.handleProxiedResponse, response, request, controls)
        def replyAndLinkToNextEntry(result):
            dseq.pop(0)
            reply(result)
            if len(dseq) > 0:
                dseq[0].addCallback(replyAndLinkToNextEntry)
        dseq.append(d)
        if len(dseq) == 1:
            d.addCallback(replyAndLinkToNextEntry)
        return isinstance(response, (
            pureldap.LDAPSearchResultDone,
            pureldap.LDAPBindResponse,
            ))

    def handleProxiedResponse(self, response, request, controls):
        """
        Override to intercept and modify proxied responses.
        Must return the modified response or a deferred that fires the modified response.
        """
        return defer.succeed(response)

    def handleUnknown(self, request, controls, reply):
        """
        Forwards requests to the proxied server.
        This handler is overridden from `ldaptor.protocol.ldap.server.BaseServer`.
        And request for which no corresponding `handle_xxx()` method is 
        implemented is dispatched to this handler.
        """
        d = defer.succeed(request)
        d.addCallback(self._forwardRequestToProxiedServer, controls, reply)
        return d

    def handle_LDAPExtendedRequest(self, request, controls, reply):
        """
        Handler for extended LDAP requests (e.g. startTLS).
        """
        if self.debug:
            log.msg("Received extended request: " + request.requestName)
        if request.requestName == pureldap.LDAPStartTLSRequest.oid:
            d = defer.maybeDeferred(self.handleStartTLSRequest, request, controls, reply)
            d.addErrback(log.err)
            return d
        return self.handleUnknown(request, controls, reply)

    def handleStartTLSRequest(self, request, controls, reply):
        """
        If the protocol factory has an `options` attribute it is assumed
        to be a `twisted.internet.ssl.CertificateOptions` that can be used
        to initiate TLS on the transport.

        Otherwise, this method returns an `unavailable` result code.
        """
        debug_flag = self.debug
        if debug_flag:
            log.msg("Received startTLS request: " + repr(request)) 
        if hasattr(self.factory, 'options'):
            if self.startTLS_initiated:
                msg = pureldap.LDAPStartTLSResponse(resultCode=ldaperrors.LDAPOperationsError.resultCode)
                log.msg("Session already using TLS.  Responding with 'operationsError' (1): " + repr(msg))
            else:
                if debug_flag:
                    log.msg("Setting success result code ...")
                msg = pureldap.LDAPStartTLSResponse(resultCode=ldaperrors.Success.resultCode)
                if debug_flag:
                    log.msg("Replying with successful LDAPStartTLSResponse ...")
                reply(msg)
                if debug_flag:
                    log.msg("Initiating startTLS on transport ...")
                self.transport.startTLS(self.factory.options)
                self.startTLS_initiated = True
                msg = None
        else:
            msg = pureldap.LDAPStartTLSResponse(resultCode=ldaperrors.LDAPUnavailable.resultCode)
            log.msg("StartTLS not implemented.  Responding with 'unavailable' (52): " + repr(msg))
        return defer.succeed(msg)
        
    def handle_LDAPUnbindRequest(self, request, controls, reply):
        """
        The client has requested to gracefully end the connection.
        Disconnect from the proxied server.
        """
        self.unbound = True
        self.handleUnknown(request, controls, reply)

class MyProxy(ProxyBase):
    """
    A simple example of using `ProxyBase` to log responses.
    """
    def handleProxiedResponse(self, response, request, controls):
        """
        Log the representation of the responses received.
        """
        log.msg("Received response from proxied service: " + repr(response))
        return defer.succeed(response)

if __name__ == '__main__':
    """
    Demonstration LDAP proxy; passes all requests to localhost:389.
    """
    from twisted.internet import protocol
    import sys
    log.startLogging(sys.stderr)
    factory = protocol.ServerFactory()
    proxied = ('localhost', 8080)
    use_tls = False
    cfg = config.LDAPConfig(serviceLocationOverrides={'': proxied, })
    factory.protocol = lambda : MyProxy(cfg, use_tls=use_tls)
    reactor.listenTCP(10389, factory)
    reactor.run()

