"""
A small client illustrating how to interact with the Sage Cell Server, version 2

Requires the websocket-client package: http://pypi.python.org/pypi/websocket-client
"""

import websocket
import threading
import json
import urllib2

class SageCell(object):
    def __init__(self, url, timeout=10):
        if not url.endswith('/'):
            url+='/'
        # POST or GET <url>/kernel
        # if there is a terms of service agreement, you need to
        # indicate acceptance in the data parameter below (see the API docs)
        req = urllib2.Request(url=url+'kernel', data='accepted_tos=true',headers={'Accept': 'application/json'})
        response = json.loads(urllib2.urlopen(req).read())

        # RESPONSE: {"kernel_id": "ce20fada-f757-45e5-92fa-05e952dd9c87", "ws_url": "ws://localhost:8888/"}
        # construct the iopub and shell websocket channel urls from that

        self.kernel_url = response['ws_url']+'kernel/'+response['kernel_id']+'/'
        websocket.setdefaulttimeout(timeout)
        print self.kernel_url
        self._shell = websocket.create_connection(self.kernel_url+'shell')
        self._iopub = websocket.create_connection(self.kernel_url+'iopub')

        # initialize our list of messages
        self.shell_messages = []
        self.iopub_messages = []

    def execute_request(self, code):
        # zero out our list of messages, in case this is not the first request
        self.shell_messages = []
        self.iopub_messages = []

        # We use threads so that we can simultaneously get the messages on both channels.
        threads = [threading.Thread(target=self._get_iopub_messages), 
                    threading.Thread(target=self._get_shell_messages)]
        for t in threads:
            t.start()

        # Send the JSON execute_request message string down the shell channel
        msg = self._make_execute_request(code)
        self._shell.send(msg)

        # Wait until we get both a kernel status idle message and an execute_reply message
        for t in threads:
            t.join()

        return {'shell': self.shell_messages, 'iopub': self.iopub_messages}

    def _get_shell_messages(self):
        while True:
            msg = json.loads(self._shell.recv())
            self.shell_messages.append(msg)
            # an execute_reply message signifies the computation is done
            if msg['header']['msg_type'] == 'execute_reply':
                break

    def _get_iopub_messages(self):
        while True:
            msg = json.loads(self._iopub.recv())
            self.iopub_messages.append(msg)
            # the kernel status idle message signifies the kernel is done
            if msg['header']['msg_type'] == 'status' and msg['content']['execution_state'] == 'idle':
                break

    def _make_execute_request(self, code):
        from uuid import uuid4
        import json
        session = str(uuid4())

        # Here is the general form for an execute_request message
        execute_request = {'header': {'msg_type': 'execute_request', 'msg_id': str(uuid4()), 'username': '', 'session': session},
                            'parent_header':{},
                            'metadata': {},
                            'content': {'code': code, 'silent': False, 'user_variables': [], 'user_expressions': {'_sagecell_files': 'sys._sage_.new_files()'}, 'allow_stdin': False}}

        return json.dumps(execute_request)

    def close(self):
        # If we define this, we can use the closing() context manager to automatically close the channels
        self._shell.close()
        self._iopub.close()

if __name__ == "__main__":
    import sys
    # argv[1] is the web address
    a=SageCell(sys.argv[1])
    import pprint
    pprint.pprint(a.execute_request('factorial(2012)'))
