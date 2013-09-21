import os, json, copy, base64, time
from twisted.trial import unittest
from twisted.web import http
from twisted.web.test.test_web import DummyRequest # not exactly stable
from .common import TwoNodeMixin
from .. import rrid
from ..eventual import flushEventualQueue
from ..mailbox import delivery, retrieval
from .test_eventsource import parse_events

class HelperMixin:
    def prepare(self):
        nA, nB = self.make_nodes(transport="local")
        return nB

    def add_recipient(self, n):
        ms = n.mailbox_server
        row = n.db.execute("SELECT * FROM mailbox_server_config").fetchone()
        sc = json.loads(row["private_descriptor_json"])
        TID_pubkey = sc["TID_public_key"].decode("hex")
        TID1_tokenid, TID1_token0 = rrid.create_token(TID_pubkey)
        STID1 = rrid.randomize(TID1_token0)

        symkey = os.urandom(32)
        tid = ms.add_TID(TID1_tokenid, symkey)

        transport_pubkey = ms.get_sender_descriptor()["transport_pubkey"]
        trec = {"STID": STID1.encode("hex"),
                "transport_pubkey": transport_pubkey}
        return tid, trec

class Inbound(HelperMixin, TwoNodeMixin, unittest.TestCase):
    def test_unknown_TID(self):
        nA, nB, entA, entB = self.make_connected_nodes(transport="local")
        msgC = "msgC"
        trec = json.loads(entA["their_channel_record_json"])["transports"][0]
        bad_trec = copy.deepcopy(trec)
        TID_privkey, TID_pubkey = rrid.create_keypair()
        TID_tokenid, TID_token0 = rrid.create_token(TID_pubkey)
        bad_STID = rrid.randomize(TID_token0)
        bad_trec["STID"] = bad_STID.encode("hex")
        msgA = delivery.createMsgA(bad_trec, msgC)

        unknowns = []
        server = nB.client.mailbox_server
        server.signal_unrecognized_TID = unknowns.append
        server.handle_msgA(msgA)
        d = flushEventualQueue()
        def _then(res):
            self.failUnlessEqual(len(unknowns), 1)
            c = nB.db.execute("SELECT * FROM mailbox_server_messages")
            messages = c.fetchall()
            self.failUnlessEqual(len(messages), 0)
        d.addCallback(_then)
        return d

    def test_local_TID(self):
        nA, nB, entA, entB = self.make_connected_nodes(transport="local")
        msgC = "msgC"
        trec = json.loads(entA["their_channel_record_json"])["transports"][0]
        msgA = delivery.createMsgA(trec, msgC)
        server = nB.client.mailbox_server
        local_messages = []
        server.local_transport_handler = local_messages.append
        server.handle_msgA(msgA)
        d = flushEventualQueue()
        def _then(res):
            c = nB.db.execute("SELECT * FROM mailbox_server_messages")
            messages = c.fetchall()
            self.failUnlessEqual(len(messages), 0)
            self.failUnlessEqual(len(local_messages), 1)
            self.failUnlessEqual(local_messages[0], msgC)
        d.addCallback(_then)
        return d

    def test_nonlocal_TID(self):
        n = self.prepare()
        msgC = "msgC"
        server = n.client.mailbox_server

        tid, trec = self.add_recipient(n)
        msgA = delivery.createMsgA(trec, msgC)

        local_messages = []
        server.local_transport_handler = local_messages.append
        server.handle_msgA(msgA)

        d = flushEventualQueue()
        def _then(res):
            self.failUnlessEqual(len(local_messages), 0)
            c = n.db.execute("SELECT * FROM mailbox_server_messages")
            messages = c.fetchall()
            self.failUnlessEqual(len(messages), 1)
            self.failUnlessEqual(messages[0]["tid"], tid)
            self.failUnlessEqual(messages[0]["length"], len(msgC))
            self.failUnlessEqual(messages[0]["msgC"].decode("hex"), msgC)
        d.addCallback(_then)
        return d

    def test_two_nonlocal_TID(self):
        n = self.prepare()
        tid1, trec1 = self.add_recipient(n)
        tid2, trec2 = self.add_recipient(n)

        msg1 = delivery.createMsgA(trec1, "msgC1")
        n.mailbox_server.handle_msgA(msg1)

        msg2 = delivery.createMsgA(trec2, "msgC2")
        n.mailbox_server.handle_msgA(msg2)

        d = flushEventualQueue()

        def _then(_):
            c = n.db.execute("SELECT * FROM mailbox_server_messages"
                             " WHERE tid=?", (tid1,))
            messages = c.fetchall()
            self.failUnlessEqual(len(messages), 1)
            self.failUnlessEqual(messages[0]["msgC"].decode("hex"), "msgC1")

            c = n.db.execute("SELECT * FROM mailbox_server_messages"
                             " WHERE tid=?", (tid2,))
            messages = c.fetchall()
            self.failUnlessEqual(len(messages), 1)
            self.failUnlessEqual(messages[0]["msgC"].decode("hex"), "msgC2")
        d.addCallback(_then)
        return d

def do_request(resource, t=None, method="GET"):
    req = DummyRequest([])
    req.method = method
    req.args = {}
    if t:
        req.args["t"] = [t]
    req.render(resource)
    return "".join(req.written), req

class Retrieval(HelperMixin, TwoNodeMixin, unittest.TestCase):
    def test_web(self):
        n = self.prepare()
        ms = n.mailbox_server
        tid1, trec1 = self.add_recipient(n)
        tid2, trec2 = self.add_recipient(n)
        TID1, symkey1 = ms.get_tid_data(tid1)
        TID2, symkey2 = ms.get_tid_data(tid2)

        ms.insert_msgC(tid1, "msgC1_first")
        ms.insert_msgC(tid1, "msgC1_second")
        ms.insert_msgC(tid2, "msgC2")

        c = n.db.execute("SELECT * FROM mailbox_server_messages"
                         " WHERE tid=?", (tid1,))
        messages = c.fetchall()
        self.failUnlessEqual(len(messages), 2)
        bodies = set([m["msgC"].decode("hex") for m in messages])
        self.failUnlessEqual(bodies, set(["msgC1_first", "msgC1_second"]))

        listres = ms.listres

        transport_pubkey = ms.get_sender_descriptor()["transport_pubkey"].decode("hex")
        reqkey, tmppub = retrieval.encrypt_list_request(transport_pubkey, TID1)

        # test 'list'
        out,req = do_request(listres, base64.urlsafe_b64encode(reqkey))
        # that created the tokens, so now we check the DB
        messages = n.db.execute("SELECT * FROM mailbox_server_messages"
                                " WHERE tid=? ORDER BY id",
                                (tid1,)).fetchall()
        self.failUnlessEqual(len(messages), 2)

        self.failUnless(out.startswith("data: "), out)
        self.failUnless(out.endswith("\n\n"), out)
        fields = parse_events(out)
        self.failUnlessEqual(len(fields), 1)
        self.failUnlessEqual(fields[0][0], "data")
        responses = fields[0][1].split()

        r1 = base64.b64decode(responses[0])
        (fetch_token1, delete_token1, length1) = \
                       retrieval.decrypt_list_entry(r1, symkey1, tmppub)
        self.failUnlessEqual(length1, len("msgC1_first"))
        self.failUnlessEqual(str(messages[0]["fetch_token"]),
                             fetch_token1.encode("hex"))
        self.failUnlessEqual(str(messages[0]["delete_token"]),
                             delete_token1.encode("hex"))
        self.failUnlessEqual(messages[0]["length"], length1)

        r2 = base64.b64decode(responses[1])
        (fetch_token2, delete_token2,
         length2) = retrieval.decrypt_list_entry(r2, symkey1, tmppub)
        self.failUnlessEqual(length2, len("msgC1_second"))
        self.failUnlessEqual(str(messages[1]["fetch_token"]),
                             fetch_token2.encode("hex"))
        self.failUnlessEqual(str(messages[1]["delete_token"]),
                             delete_token2.encode("hex"))
        self.failUnlessEqual(messages[1]["length"], length2)

        r = n.web.get_root().getStaticEntity("retrieval")
        fetchres = r.getStaticEntity("fetch")
        deleteres = r.getStaticEntity("delete")

        # test 'fetch' on the first message
        out, req = do_request(fetchres,
                              base64.urlsafe_b64encode(fetch_token1))
        encrypted_m1 = base64.b64decode(out)
        m1 = retrieval.decrypt_fetch_response(symkey1, fetch_token1,
                                              encrypted_m1)
        self.failUnlessEqual(m1, "msgC1_first")

        # fetch_token is single-use
        out, req = do_request(fetchres,
                              base64.urlsafe_b64encode(fetch_token1))
        self.failUnlessEqual(req.responseCode, http.NOT_FOUND)
        self.failUnlessEqual(req.responseMessage, "unknown fetch_token")
        self.failUnlessEqual(out, "")
        messages = n.db.execute("SELECT * FROM mailbox_server_messages"
                                " WHERE tid=? ORDER BY id",
                                (tid1,)).fetchall()
        self.failUnlessEqual(len(messages), 2)
        self.failUnlessEqual(messages[0]["fetch_token"], None)

        # replay should be rejected
        out,req = do_request(listres, base64.urlsafe_b64encode(reqkey))
        self.failUnlessEqual(req.responseCode, http.BAD_REQUEST)

        # test unknown tokens
        out,req = do_request(fetchres, base64.urlsafe_b64encode("wrong"))
        self.failUnlessEqual(req.responseCode, 404)
        self.failUnlessEqual(req.responseMessage, "unknown fetch_token")
        self.failUnlessEqual(out, "")

        # test delete_token
        out, req = do_request(deleteres,
                              base64.urlsafe_b64encode(delete_token1),
                              method="POST")
        self.failUnlessEqual(req.responseCode, http.OK)
        self.failUnlessEqual(out, "")
        messages = n.db.execute("SELECT * FROM mailbox_server_messages"
                                " WHERE tid=? ORDER BY id",
                                (tid1,)).fetchall()
        self.failUnlessEqual(len(messages), 1)

        # timestamp in past (old replay)
        now = time.time()
        past = now - 24*3600
        future = now + 24*3600
        reqkey, tmppub = retrieval.encrypt_list_request(transport_pubkey, TID1,
                                                        now=past)
        out,req = do_request(listres, base64.urlsafe_b64encode(reqkey))
        self.failUnlessEqual(req.responseCode, http.BAD_REQUEST)
        self.failUnlessEqual(req.responseMessage, "too much clock skew")
        self.failUnlessEqual(out, "Too much clock skew")

        # timestamp in future
        reqkey, tmppub = retrieval.encrypt_list_request(transport_pubkey, TID1,
                                                        now=future)
        out,req = do_request(listres, base64.urlsafe_b64encode(reqkey))
        self.failUnlessEqual(req.responseCode, http.BAD_REQUEST)
        self.failUnlessEqual(req.responseMessage, "too much clock skew")
        self.failUnlessEqual(out, "Too much clock skew")

        self.failUnlessEqual(len(listres.old_requests), 1)
        listres.prune_old_requests(now=future)
        self.failUnlessEqual(len(listres.old_requests), 0)

        # TODO: unrecognized TID, causing KeyError (or nicer)

        # TODO: a second 'list' should revoke tokens from the first

        # TODO: streaming/EventSource, new messages should trigger events

        #reqkey, tmppub = retrieval.encrypt_list_request(transport_pubkey, TID1)
