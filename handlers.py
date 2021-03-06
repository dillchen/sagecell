import time, string, urllib, zlib, base64, uuid, json, os.path, re

import tornado.web
import tornado.websocket
import tornado.gen as gen
import sockjs.tornado
from zmq.eventloop import ioloop
from zmq.utils import jsonapi
import math
try:
    from IPython.kernel.zmq.session import Session
except ImportError:
    # old IPython
    from IPython.zmq.session import Session
try:
    from sage.all import gap, gp, maxima, r, singular
    trait_names = {
        "gap": gap.trait_names(),
        "gp": gp.trait_names(),
        "maxima": maxima.trait_names(),
        "r": r.trait_names(),
        "singular": singular.trait_names()
    }
except ImportError:
    trait_names = {}
from misc import sage_json, Timer, Config
config = Config()
import logging
logger = logging.getLogger('sagecell')

import re
cron = re.compile("^print [-]?\d+\+[-]?\d+$")


statslogger = logging.getLogger('sagecell.stats')
statslogger.propagate=False
import sys
#h = logging.FileHandler('stats.log','a')
h = logging.StreamHandler(sys.stdout)
h.setFormatter(logging.Formatter('%(asctime)s %(name)s %(process)d: %(message)s'))
statslogger.addHandler(h)
from log import StatsMessage

class RootHandler(tornado.web.RequestHandler):
    """
    Root URL request handler.
    
    This renders templates/root.html, which optionally inserts
    specified preloaded code during the rendering process.
    
    There are three ways currently supported to specify
    preloading code:
    
    ``<root_url>?c=<code>`` loads 'plaintext' code
    ``<root_url>?z=<base64>`` loads base64-compressed code
    ```<root_url>?q=<uuid>`` loads code from a database based
    upon a unique identifying permalink (uuid4-based)
    """
    @tornado.web.asynchronous
    def get(self):
        logger.debug('request')
        db = self.application.db
        code = None
        language = None
        interacts = None
        args = self.request.arguments

        if "lang" in args:
            language = args["lang"][0]

        if "c" in args:
            # If the code is explicitly specified
            code = "".join(args["c"])

        elif "z" in args:
            # If the code is base64-compressed
            try:
                z = "".join(args["z"])
                # We allow the user to strip off the ``=`` padding at the end
                # so that the URL doesn't have to have any escaping.
                # Here we add back the ``=`` padding if we need it.
                z += "=" * ((4 - (len(z) % 4)) % 4)
                if "interacts" in args:
                    interacts = "".join(args["interacts"])
                    interacts += "=" * ((4 - (len(interacts) % 4)) % 4)
                    interacts = zlib.decompress(base64.urlsafe_b64decode(interacts))
                else:
                    interacts = "[]"
                code = zlib.decompress(base64.urlsafe_b64decode(z))
            except Exception as e:
                self.set_status(400)
                self.finish("Invalid zipped code: %s\n" % (e.message,))
                return

        if "q" in args:
            # if the code is referenced by a permalink identifier
            q = "".join(args["q"])
            try:
                db.get_exec_msg(q, self.return_root)
            except LookupError:
                self.set_status(404)
                self.finish("ID not found in permalink database")
                return
        else:
            self.return_root(code, language, interacts)

    def return_root(self, code, language, interacts):
        autoeval = None
        if code is not None:
            if isinstance(code, unicode):
                code = code.encode("utf8")
            code = urllib.quote(code)
            autoeval = "false" if "autoeval" in self.request.arguments and self.get_argument("autoeval") == "false" else "true"
        if interacts == "[]":
            interacts = None
        if interacts is not None:
            if isinstance(interacts, unicode):
                interacts = interacts.encode("utf8")
            interacts = urllib.quote(interacts)
        self.render("root.html", code=code, lang=language, interacts=interacts, autoeval=autoeval)

class KernelHandler(tornado.web.RequestHandler):
    """
    Kernel startup request handler.
    
    This starts up an IPython kernel on an untrusted account
    and returns the associated kernel id and a url to request
    websocket connections for a websocket-ZMQ bridge back to
    the kernel in a JSON-compatible message.
    
    The returned websocket url is not entirely complete, in
    that it is the base url to be used for two different
    websocket connections (corresponding to the shell and
    iopub streams) of the IPython kernel. It is the
    responsiblity of the client to request the correct URLs
    for these websockets based on the following pattern:
    
    ``<ws_url>/iopub`` is the expected iopub stream url
    ``<ws_url>/shell`` is the expected shell stream url
    """
    @tornado.web.asynchronous
    @gen.engine
    def post(self, *args, **kwargs):
        method = self.get_argument("method", "POST")
        if method == "DELETE":
            self.delete(*args, **kwargs)
        elif method == "OPTIONS":
            self.options(*args, **kwargs)
        else:
            if config.get_config("requires_tos") and self.get_cookie("accepted_tos") != "true" and \
                self.get_argument("accepted_tos", "false") != "true":
                self.set_status(403)
                self.finish()
                return
            timer = Timer("Kernel handler for %s"%self.get_argument("notebook", uuid.uuid4()))
            proto = self.request.protocol.replace("http", "ws", 1)
            host = self.request.host
            ws_url = "%s://%s/" % (proto, host)
            km = self.application.km
            logger.info("Starting session: %s"%timer)
            timeout = self.get_argument("timeout", None)
            if timeout is not None:
                timeout = float(timeout)
                if math.isnan(timeout) or timeout<0:
                    timeout = None
            kernel_id = yield gen.Task(km.new_session_async,
                                       referer = self.request.headers.get('Referer',''),
                                       remote_ip = self.request.remote_ip,
                                       timeout = timeout)
            data = {"ws_url": ws_url, "id": kernel_id}
            self.write(self.permissions(data))
            self.set_cookie("accepted_tos", "true", expires_days=365)
            self.finish()


    def delete(self, kernel_id):
        self.application.km.end_session(kernel_id)
        self.permissions()
        self.finish()

    def options(self, kernel_id):
        logger.info("options kernel: %s",kernel_id)
        self.permissions()
        self.finish()

    def permissions(self, data=None):
        if "frame" not in self.request.arguments:
            self.set_header("Access-Control-Allow-Origin", self.request.headers.get("Origin", "*"))
            self.set_header("Access-Control-Allow-Credentials", "true")
            self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS, DELETE")
        else:
            data = '<script>parent.postMessage(%r,"*");</script>' % (json.dumps(data),)
            self.set_header("Content-Type", "text/html")
        return data

class Completer(object):
    name_pattern = re.compile(r"\b[a-z_]\w*$", re.IGNORECASE)

    def __init__(self, km):
        self.waiting = {}
        self.kernel_id = km.new_session(limited=False)
        self.session = km._sessions[self.kernel_id]
        self.stream = km.create_shell_stream(self.kernel_id)
        self.stream.on_recv(self.on_recv)

    def registerRequest(self, kc, msg):
        name = None
        if "mode" not in msg["content"] or msg["content"]["mode"] in ("sage", "python"):
            self.waiting[msg["header"]["msg_id"]] = kc
            self.session.send(self.stream, msg)
            return
        elif msg["content"]["mode"] in trait_names:
            line = msg["content"]["line"][:msg["content"]["cursor_pos"]]
            name = Completer.name_pattern.search(line)
        response = {
            "header": {
                "msg_id": str(uuid.uuid4()),
                "username": "",
                "session": self.kernel_id,
                "msg_type": "complete_reply"
            },
            "parent_header": msg["header"],
            "metadata": {}
        }
        if name is not None:
            response["content"] = {
                "matches": [t for t in trait_names[msg["content"]["mode"]] if t.startswith(name.group())],
                "matched_text": name.group()
            }
        else:
            response["content"] = {
                "matches": [],
                "matched_text": []
            }
        kc.send("complete/shell," + jsonapi.dumps(response))

    def on_recv(self, msg):
        msg = self.session.feed_identities(msg)[1]
        msg = self.session.unserialize(msg)
        msg_id = msg["parent_header"]["msg_id"]
        kc = self.waiting.pop(msg_id)
        del msg["header"]["date"]
        kc.send("complete/shell," + jsonapi.dumps(msg))

class KernelConnection(sockjs.tornado.SockJSConnection):
    def __init__(self, session):
        super(KernelConnection, self).__init__(session)

    def on_open(self, request):
        self.channels = {}

    def on_message(self, message):
        prefix, message = message.split(",", 1)
        kernel, channel = prefix.split("/", 1)
        if channel=="stdin":
            # TODO: Support the stdin channel
            # See http://ipython.org/ipython-doc/dev/development/messaging.html
            return
        try:
            if kernel == "complete":
                application = self.session.handler.application
                message = jsonapi.loads(message)
                if message["header"]["msg_type"] in ("complete_request", "object_info_request"):
                    application.completer.registerRequest(self, message)
            elif kernel not in self.channels:
                # handler may be None in certain circumstances (it seems to only be set
                # in GET requests, not POST requests, so even using it here may
                # only work with JSONP because of a race condition)
                application = self.session.handler.application
                kernel_info = application.km.kernel_info(kernel)
                self.kernel_info = {'remote_ip': kernel_info['remote_ip'],
                                    'referer': kernel_info['referer'],
                                    'timeout': kernel_info['timeout']}
                self.channels[kernel] = \
                    {"shell": ShellSockJSHandler(kernel, self.send, application),
                     "iopub": IOPubSockJSHandler(kernel, self.send, application)}
                self.channels[kernel]["shell"].open(kernel)
                self.channels[kernel]["iopub"].open(kernel)
            if kernel != "complete":
                self._log_stats(kernel, message)
                self.channels[kernel][channel].on_message(message)
        except KeyError:
            jsonmessage=jsonapi.loads(message)
            logger.info("%s message sent to deleted kernel: %s"%(jsonmessage["header"]["msg_type"], kernel))
            pass # Ignore messages to nonexistant or killed kernels

    def on_close(self):
        for channel in self.channels.itervalues():
            channel["shell"].on_close()
            channel["iopub"].on_close()

    def _log_stats(self, kernel, msg):
        msg=json.loads(msg)
        if msg["header"]["msg_type"] == "execute_request":
            statslogger.info(StatsMessage(kernel_id = kernel,
                                          remote_ip = self.kernel_info['remote_ip'],
                                          referer = self.kernel_info['referer'],
                                          code = msg["content"]["code"],
                                          execute_type='request'))

KernelRouter = sockjs.tornado.SockJSRouter(KernelConnection, "/sockjs")

class TOSHandler(tornado.web.RequestHandler):
    """Handler for ``/tos.html``"""
    tos = config.get_config("requires_tos")
    if tos:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "tos.html")
        with open(path) as f:
            tos_html = f.read()
            tos_json = json.dumps(tos_html)
    else:
        tos_html = "No Terms of Service Required"
        tos_json = json.dumps(tos_html)
    
    def post(self):
        cookie_set = self.get_cookie("accepted_tos") == "true" or not self.tos
        if len(self.get_arguments("callback")) == 0:
            if cookie_set:
                self.set_status(204)
            else:
                self.write(self.tos_html)
            self.set_header("Access-Control-Allow-Origin", self.request.headers.get("Origin", "*"))
            self.set_header("Access-Control-Allow-Credentials", "true")
            self.set_header("Content-Type", "text/html")
        else:
            resp = '""' if cookie_set else self.tos_json
            self.write("%s(%s);" % (self.get_argument("callback"), resp))
            self.set_header("Content-Type", "application/javascript")

    def get(self):
        if self.tos:
            self.write(self.tos_html)
        else:
            raise tornado.web.HTTPError(404, 'No Terms of Service Required')

class SageCellHandler(tornado.web.RequestHandler):
    """Handler for ``/sagecell.html``"""

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "sagecell.html")) as f:
        sagecell_html = f.read()
        sagecell_json = json.dumps(sagecell_html)

    def get(self):
        if len(self.get_arguments("callback")) == 0:
            self.write(self.sagecell_html);
            self.set_header("Access-Control-Allow-Origin", self.request.headers.get("Origin", "*"))
            self.set_header("Access-Control-Allow-Credentials", "true")
            self.set_header("Content-Type", "text/html")
        else:
            self.write("%s(%s);" % (self.get_argument("callback"), self.sagecell_json))
            self.set_header("Content-Type", "application/javascript")

class StaticHandler(tornado.web.StaticFileHandler):
    """Handler for static requests"""
    def set_extra_headers(self, path):
        self.set_header("Access-Control-Allow-Origin", self.request.headers.get("Origin", "*"))
        self.set_header("Access-Control-Allow-Credentials", "true")

class ServiceHandler(tornado.web.RequestHandler):
    """
    Implements a blocking (to the client) web service to execute a single
    computation the server.  This should be non-blocking to Tornado.

    The code to be executed is given in the code request parameter.

    This handler is currently not production-ready.
    """
    @tornado.web.asynchronous
    @gen.engine
    def post(self):
        if config.get_config("requires_tos") and self.get_cookie("accepted_tos") != "true" and \
            self.get_argument("accepted_tos", "false") != "true":
            self.write("""When evaluating code, you must acknowledge your acceptance
of the terms of service at /static/tos.html by passing the parameter or cookie
accepted_tos=true\n""")
            self.set_status(403)
            self.finish()
            return
        default_timeout = 30 # seconds
        code = "".join(self.get_arguments('code', strip=False))
        if len(code)>65000:
            self.set_status(413)
            self.write("Max code size is 65000 characters")
            self.finish()
            return
        if code:
            km = self.application.km
            remote_ip = self.request.remote_ip
            referer = self.request.headers.get('Referer','')
            self.kernel_id = yield gen.Task(km.new_session_async,
                                            referer = referer,
                                            remote_ip = remote_ip,
                                            timeout=0)
            if not (remote_ip=="::1" and referer==""
                    and cron.match(code) is not None):
                statslogger.info(StatsMessage(kernel_id = self.kernel_id,
                                              remote_ip = remote_ip,
                                              referer = referer,
                                              code = code,
                                              execute_type = 'service'))

            self.shell_handler = ShellServiceHandler(self.application)
            self.iopub_handler = IOPubServiceHandler(self.application)
            
            self.shell_handler.open(self.kernel_id)
            self.iopub_handler.open(self.kernel_id)

            loop = ioloop.IOLoop.instance()

            self.success = False
            def done(msg):
                if msg["msg_type"] == "execute_reply":
                    self.success = msg["content"]["status"] == "ok"
                    self.user_variables = msg["content"].get("user_variables", [])
                    self.execute_reply = msg['content']
                    loop.remove_timeout(self.timeout_request)
                    loop.add_callback(self.finish_request)
            self.shell_handler.msg_from_kernel_callbacks.append(done)
            self.timeout_request = loop.add_timeout(time.time()+default_timeout, self.finish_request)
            exec_message = {"parent_header": {},
                            "header": {"msg_id": str(uuid.uuid4()),
                                       "username": "",
                                       "session": self.kernel_id,
                                       "msg_type": "execute_request",
                                       },
                            "content": {"code": code,
                                        "silent": False,
                                        "user_variables": self.get_arguments('user_variables'),
                                        "user_expressions": {},
                                        "allow_stdin": False,
                                        },
                            "metadata": {}
                            }
            self.shell_handler.on_message(jsonapi.dumps(exec_message))

    def finish_request(self):
        try: # in case kernel has already been killed
            self.application.km.end_session(self.kernel_id)
        except:
            pass

        #statslogger.info(StatMessage(kernel_id = self.kernel_id, '%r SERVICE DONE'%self.kernel_id)
        retval = self.iopub_handler.streams
        self.shell_handler.on_close()
        self.iopub_handler.on_close()
        retval.update(success=self.success)
        retval.update(user_variables=self.user_variables)
        retval.update(execute_reply=self.execute_reply)
        self.set_header("Access-Control-Allow-Origin", self.request.headers.get("Origin", "*"))
        self.set_header("Access-Control-Allow-Credentials", "true")
        self.write(retval)
        self.finish()

class ZMQStreamHandler(object):
    """
    Base class for a websocket-ZMQ bridge using ZMQStream.

    At minimum, subclasses should define their own ``open``,
    ``on_close``, and ``on_message` functions depending on
    what type of ZMQStream is used.
    """
    def open(self, kernel_id):
        self.km = self.application.km
        self.kernel_id = kernel_id
        self.session = self.km._sessions[self.kernel_id]
        self.kernel = self.km._kernels[self.kernel_id]
        self.msg_from_kernel_callbacks = []
        self.msg_to_kernel_callbacks = []

    def _unserialize_reply(self, msg_list):
        """
        Converts a multipart list of received messages into
        one coherent JSON message.
        """
        idents, msg_list = self.session.feed_identities(msg_list)
        return self.session.unserialize(msg_list)

    def _json_msg(self, msg):
        """
        Converts a single message into a JSON string
        """
        # can't encode buffers, so let's get rid of them if they exist
        msg.pop("buffers", None)
        # sage_json handles things like encoding dates and sage types
        return jsonapi.dumps(msg, default=sage_json)

    def _on_zmq_reply(self, msg_list):
        try:
            msg = self._unserialize_reply(msg_list)
            send = True
            for f in self.msg_from_kernel_callbacks:
                result = f(msg)
                if result is False:
                    send = False
            if send:
                self._output_message(msg)
        except:
            pass
    
    def _output_message(self, message):
        raise NotImplementedError

    def on_close(self):
        self.km.end_session(self.kernel_id)

class ShellHandler(ZMQStreamHandler):
    """
    This handles the websocket-ZMQ bridge for the shell
    stream of an IPython kernel.
    """
    def open(self, kernel_id):
        super(ShellHandler, self).open(kernel_id)
        self.kill_kernel = False
        self.shell_stream = self.km.create_shell_stream(self.kernel_id)
        self.shell_stream.on_recv(self._on_zmq_reply)
        self.msg_from_kernel_callbacks.append(self._reset_deadline)

    def _reset_deadline(self, msg):
        if msg["header"]["msg_type"] in ("execute_reply",
                                         "sagenb.interact.update_interact_reply"):
            timeout = self.kernel["timeout"]
            if timeout > self.km.max_kernel_timeout:
                self.kernel["timeout"] = timeout = self.km.max_kernel_timeout
            if timeout <= 0.0 and self.kernel["executing"] == 1:
                # kill the kernel before the heartbeat is able to
                self.kill_kernel = True
            else:
                self.kernel["deadline"] = (time.time()+timeout)
                self.kernel["executing"] -= 1

    def on_message(self, message):
        if self.km._kernels.get(self.kernel_id) is not None:
            msg = jsonapi.loads(message)
            for f in self.msg_to_kernel_callbacks:
                f(msg)
            self.kernel["executing"] += 1
            self.session.send(self.shell_stream, msg)

    def on_close(self):
        if self.shell_stream is not None and not self.shell_stream.closed():
            self.shell_stream.close()
        super(ShellHandler, self).on_close()

    def _on_zmq_reply(self, msg_list):
        """
        After receiving a kernel's final execute_reply, immediately kill the kernel
        and send that status to the client (rather than waiting for the message to
        be sent after the heartbeat fails. This prevents the user from attempting to
        execute code in a kernel between the time that the kernel is killed
        and the time that the browser receives the "kernel killed" message.
        """
        super(ShellHandler, self)._on_zmq_reply(msg_list)
        if self.kill_kernel:
            self.shell_stream.flush()
            self.kernel["kill"]()

class IOPubHandler(ZMQStreamHandler):
    """
    This handles the websocket-ZMQ bridge for the iopub
    stream of an IPython kernel. It also handles the
    heartbeat (hb) stream that same kernel, but there is no
    associated websocket connection. The iopub websocket is
    instead used to notify the client if the heartbeat
    stream fails.
    """
    def open(self, kernel_id):
        super(IOPubHandler, self).open(kernel_id)

        self._kernel_alive = True
        self._beating = False
        self.iopub_stream = None
        self.hb_stream = None

        self.iopub_stream = self.km.create_iopub_stream(self.kernel_id)
        self.iopub_stream.on_recv(self._on_zmq_reply)
        self.kernel["kill"] = self.kernel_died

        self.hb_stream = self.km.create_hb_stream(self.kernel_id)
        self.start_hb(self.kernel_died)

        self.msg_from_kernel_callbacks.append(self._reset_timeout)

    def _reset_timeout(self, msg):
        if msg["header"]["msg_type"]=="kernel_timeout":
            try:
                timeout = float(msg["content"]["timeout"])
                if (not math.isnan(timeout)) and timeout >= 0:
                    if timeout > self.km.max_kernel_timeout:
                        timeout = self.km.max_kernel_timeout
                    self.kernel["timeout"] = timeout
            except:
                pass
            return False
        
    def on_message(self, msg):
        pass

    def on_close(self):
        if self.iopub_stream is not None and not self.iopub_stream.closed():
            self.iopub_stream.on_recv(None)
            self.iopub_stream.close()
        if self.hb_stream is not None and not self.hb_stream.closed():
            self.stop_hb()
        super(IOPubHandler, self).on_close()

    def start_hb(self, callback):
        """
        Starts a series of delayed callbacks to send and
        receive small messages from the heartbeat stream of
        an IPython kernel. The specific delay paramaters for
        the callbacks are set by configuration values in a
        kernel manager associated with the web application.
        """
        if not self._beating:
            self._kernel_alive = True

            def ping_or_dead():
                self.hb_stream.flush()
                try:
                    if self.kernel["executing"] == 0:
                        # only kill the kernel after all pending
                        # execute requests have finished
                        if time.time() > self.kernel["deadline"]:
                            self._kernel_alive = False
                except:
                    self._kernel_alive = False

                if self._kernel_alive:
                    self._kernel_alive = False
                    self.hb_stream.send(b'ping')
                    # flush stream to force immediate socket send
                    self.hb_stream.flush()
                else:
                    try:
                        callback()
                    except:
                        pass
                    finally:
                        self.stop_hb()

            def beat_received(msg):
                self._kernel_alive = True

            self.hb_stream.on_recv(beat_received)

            loop = ioloop.IOLoop.instance()
 
            (self.beat_interval, self.first_beat) = self.km.get_hb_info(self.kernel_id)

            self._hb_periodic_callback = ioloop.PeriodicCallback(ping_or_dead, self.beat_interval*1000, loop)

            loop.add_timeout(time.time()+self.first_beat, self._really_start_hb)
            self._beating= True

    def _really_start_hb(self):
        """
        callback for delayed heartbeat start
        Only start the hb loop if we haven't been closed during the wait.
        """
        if self._beating and not self.hb_stream.closed():
            self._hb_periodic_callback.start()

    def stop_hb(self):
        """Stop the heartbeating and cancel all related callbacks."""
        if self._beating:
            self._beating = False
            self._hb_periodic_callback.stop()
            if not self.hb_stream.closed():
                self.hb_stream.on_recv(None)
                self.hb_stream.close()

    def kernel_died(self):
        try: # in case kernel has already been killed
            self.iopub_stream.flush()
            self.application.km.end_session(self.kernel_id)
        except:
            pass
        msg = {
            'header': {
                'msg_type': 'status',
                'session': self.kernel_id,
                'msg_id': str(uuid.uuid4()),
                'username': ''
            },
            'parent_header': {},
            'metadata': {},
            'content': {'execution_state': 'dead'}
        }
        self._output_message(msg)
        self.on_close()

class ShellServiceHandler(ShellHandler):
    def __init__(self, application):
        self.application = application

    def _output_message(self, message):
        pass

class IOPubServiceHandler(IOPubHandler):
    def __init__(self, application):
        self.application = application

    def open(self, kernel_id):
        super(IOPubServiceHandler, self).open(kernel_id)
        from collections import defaultdict
        self.streams = defaultdict(unicode)

    def _output_message(self, msg):
        if msg["header"]["msg_type"] == "stream":
            self.streams[msg["content"]["name"]] += msg["content"]["data"]

class ShellWebHandler(ShellHandler, tornado.websocket.WebSocketHandler):
    def _output_message(self, message):
        self.write_message(self._json_msg(message))
    def allow_draft76(self):
        """Allow draft 76, until browsers such as Safari update to RFC 6455.
        
        This has been disabled by default in tornado in release 2.2.0, and
        support will be removed in later versions.
        """
        return True

class IOPubWebHandler(IOPubHandler, tornado.websocket.WebSocketHandler):
    def _output_message(self, message):
        self.write_message(self._json_msg(message))
    def allow_draft76(self):
        """Allow draft 76, until browsers such as Safari update to RFC 6455.
        
        This has been disabled by default in tornado in release 2.2.0, and
        support will be removed in later versions.
        """
        return True

class ShellSockJSHandler(ShellHandler):
    def __init__(self, kernel_id, callback, application):
        self.kernel_id = kernel_id
        self.callback = callback
        self.application = application

    def _output_message(self, message):
        self.callback("%s/shell,%s" % (self.kernel_id, self._json_msg(message)))

class IOPubSockJSHandler(IOPubHandler):
    def __init__(self, kernel_id, callback, application):
        self.kernel_id = kernel_id
        self.callback = callback
        self.application = application

    def _output_message(self, message):
        self.callback("%s/iopub,%s" % (self.kernel_id, self._json_msg(message)))

class FileHandler(StaticHandler):
    """
    Files handler
    
    This takes in a filename and returns the file
    """
    def get(self, kernel_id, file_path):
        super(FileHandler, self).get('%s/%s'%(kernel_id, file_path))
