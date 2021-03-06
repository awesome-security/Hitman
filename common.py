#!/usr/bin/env python

import time
import threading
from scapy.all import *
import sys
import socket
import json
import Queue
import interfaces

maxhop = 25

# A request that will trigger the great firewall but will NOT cause
# the web server to process the connection.  You probably want it here

triggerfetch = "GET /search?q=Falun+Gong HTTP/1.1\r\nhost: www.google.com\r\n\r\n"
# triggerfetch = "GET / HTTP/1.1\r\nhost: www.google.com\r\n\r\n"
# triggerfetch = "GET / HTTP/1.1\r\nhost: www.miit.gov.cn\r\n\r\n"
# triggerfetch = "GET / HTTP/1.1\r\nhost: www.miit.gov.cn\r\n\r\n"
# triggerfetch = "GET / HTTP/1.1\r\nhost: www-inst.eecs.berkeley.edu\r\nconnection: close\r\n\r\n"

FIN = 0x01
SYN = 0x02
RST = 0x04
PSH = 0x08
ACK = 0x10
URG = 0x20
ECE = 0x40
CWR = 0x80

# A couple useful functions that take scapy packets
def isRST(p):
    return (TCP in p) and (p[IP][TCP].flags & 0x4 != 0)

def isICMP(p):
    return ICMP in p

def isTimeExceeded(p):
    return ICMP in p and p[IP][ICMP].type == 11

def isSYNACK(p):
    if TCP not in p:
        return False
    f = p[IP][TCP].flags
    return ((f & SYN) != 0) and ((f & ACK) != 0)

def isSYN(p):
    if TCP not in p:
        return False
    f = p[IP][TCP].flags
    return (f & SYN) != 0

def isACK(p):
    if TCP not in p:
        return False
    f = p[IP][TCP].flags
    return (f & ACK) != 0

# A general python object to handle a lot of this stuff...
#
# Use this to implement the actual functions you need.
class PacketUtils:
    def __init__(self, dst=None):
        # Get one's SRC IP & interface
        i = interfaces.interfaces()
        self.src = i[1][0]
        self.iface = i[0]
        self.netmask = i[1][1]
        self.enet = i[2]
        self.dst = dst
        sys.stderr.write("SIP IP %s, iface %s, netmask %s, enet %s\n" %
                         (self.src, self.iface, self.netmask, self.enet))
        # A queue where received packets go.  If it is full
        # packets are dropped.
        self.packetQueue = Queue.Queue(100000)
        self.dropCount = 0
        self.idcount = 0

        self.ethrdst = ""

        # Get the destination ethernet address with an ARP
        self.arp()

        # You can add other stuff in here to, e.g. keep track of
        # outstanding ports, etc.

        # Start the packet sniffer
        t = threading.Thread(target=self.run_sniffer)
        t.daemon = True
        t.start()
        time.sleep(.1)

    # generates an ARP request
    def arp(self):
        e = Ether(dst="ff:ff:ff:ff:ff:ff",
                  type=0x0806)
        gateway = ""
        srcs = self.src.split('.')
        netmask = self.netmask.split('.')
        for x in range(4):
            nm = int(netmask[x])
            addr = int(srcs[x])
            if x == 3:
                gateway += "%i" % ((addr & nm) + 1)
            else:
                gateway += ("%i" % (addr & nm)) + "."
        sys.stderr.write("Gateway %s\n" % gateway)
        a = ARP(hwsrc=self.enet,
                pdst=gateway)
        p = srp1([e/a], iface=self.iface, verbose=0)
        self.etherdst = p[Ether].src
        sys.stderr.write("Ethernet destination %s\n" % (self.etherdst))


    # A function to send an individual packet.
    def send_pkt(self, payload=None, ttl=32, flags="",
                 seq=None, ack=None,
                 sport=None, dport=80,ipid=None,
                 dip=None,debug=False):
        if sport == None:
            sport = random.randint(1024, 32000)
        if seq == None:
            seq = random.randint(1, 31313131)
        if ack == None:
            ack = random.randint(1, 31313131)
        if ipid == None:
            ipid = self.idcount
            self.idcount += 1
        t = TCP(sport=sport, dport=dport,
                flags=flags, seq=seq, ack=ack)
        ip = IP(src=self.src,
                dst=self.dst,
                id=ipid,
                ttl=ttl)
        p = ip/t
        if payload:
            p = ip/t/payload
        else:
            pass
        e = Ether(dst=self.etherdst,
                  type=0x0800)
        # Have to send as Ethernet to avoid interface issues
        sendp([e/p], verbose=1, iface=self.iface)
        # Limit to 20 PPS.
        time.sleep(.05)
        # And return the packet for reference
        return p


    # Has an automatic 5 second timeout.
    def get_pkt(self, timeout=5):
        try:
            return self.packetQueue.get(True, timeout)
        except Queue.Empty:
            return None

    # The function that actually does the sniffing
    def sniffer(self, packet):
        try:
            # non-blocking: if it fails, it fails
            self.packetQueue.put(packet, False)
        except Queue.Full:
            if self.dropCount % 1000 == 0:
                sys.stderr.write("*")
                sys.stderr.flush()
            self.dropCount += 1

    def run_sniffer(self):
        sys.stderr.write("Sniffer started\n")
        rule = "src net %s or icmp" % self.dst
        sys.stderr.write("Sniffer rule \"%s\"\n" % rule);
        sniff(prn=self.sniffer,
              filter=rule,
              iface=self.iface,
              store=0)

    # Sends the message to the target in such a way
    # that the target receives the msg without
    # interference by the Great Firewall.
    #
    # ttl is a ttl which triggers the Great Firewall but is before the
    # server itself (from a previous traceroute incantation
    def evade(self, target, msg, ttl):
        # print("ttl", ttl)
        send_port, send_seq, synack_pkt = self.handshake()
        if synack_pkt is None:
            return None

        # print(synack_pkt)
        seq_offset = 1
        chunk_size = 2
        letters = "abcdefghijklmnopqrstuvwxyz"
        while len(msg) > 0:
            payload = msg[:chunk_size]
            msg = msg[chunk_size:]
            rand_msg = ''.join([random.choice(letters) for _ in range(chunk_size)])

            if len(msg) + len(payload) > 5:
                if random.randint(0, 1) == 0:
                    pkt = self.send_pkt(
                        payload=payload,
                        flags="PA",
                        seq=send_seq + seq_offset,
                        ack=synack_pkt[IP][TCP].seq + 1,
                        sport=send_port,
                    )

                    pkt = self.send_pkt(
                        payload=rand_msg,
                        ttl=ttl+1,
                        flags="PA",
                        seq=send_seq + seq_offset,
                        ack=synack_pkt[IP][TCP].seq + 1,
                        sport=send_port,
                    )
                else:
                    pkt = self.send_pkt(
                        payload=rand_msg,
                        ttl=ttl+1,
                        flags="PA",
                        seq=send_seq + seq_offset,
                        ack=synack_pkt[IP][TCP].seq + 1,
                        sport=send_port,
                    )

                    pkt = self.send_pkt(
                        payload=payload,
                        flags="PA",
                        seq=send_seq + seq_offset,
                        ack=synack_pkt[IP][TCP].seq + 1,
                        sport=send_port,
                    )
            else:
                # Make sure the terminating new line characters get delivered
                pkt = self.send_pkt(
                    payload=payload,
                    flags="PA",
                    seq=send_seq + seq_offset,
                    ack=synack_pkt[IP][TCP].seq + 1,
                    sport=send_port,
                )

            seq_offset += len(payload)

        # Get data in first 5 seconds
        result = ''
        stop_time = time.time() + 5.0

        while (time.time() < stop_time):
            pkt = self.get_pkt(timeout=(stop_time - time.time()))
            if pkt != None and not isTimeExceeded(pkt):
                if 'Raw' in pkt:
                    result += pkt['Raw'].load

        return result

    # Returns "DEAD" if server isn't alive,
    # "LIVE" if teh server is alive,
    # "FIREWALL" if it is behind the Great Firewall
    def ping(self, target):
        # self.send_msg([triggerfetch], dst=target, syn=True)
        send_port = random.randrange(2000, 30000)
        send_seq = random.randint(1, 31313131)

        syn_pkt = self.send_pkt(
            flags="S",
            seq=send_seq,
            sport=send_port,
        )

        stop_time = time.time() + 5.0
        synack_pkt = self.get_pkt(timeout=5)
        while (time.time() < stop_time) and synack_pkt != None and not (
            isSYNACK(synack_pkt) and
            synack_pkt[IP][TCP].ack == send_seq + 1
            ):
            synack_pkt = self.get_pkt(timeout=stop_time - time.time())

        if synack_pkt is None or not (
            isSYNACK(synack_pkt) and
            synack_pkt[IP][TCP].ack == send_seq + 1
            ):
            return "DEAD"

        ack_pkt = self.send_pkt(
            payload=triggerfetch,
            flags="PA",
            ttl=32,
            seq=synack_pkt[IP][TCP].ack,
            ack=synack_pkt[IP][TCP].seq + 1,
            sport=send_port,
        )

        stop_time = time.time() + 5.0
        reply_pkt = self.get_pkt()
        if reply_pkt == None:
            return "DEAD"

        rst_returned = False
        live_handshake = False
        while (time.time() < stop_time) and (reply_pkt != None):
            if isRST(reply_pkt):
                rst_returned = True
                break
            elif not isICMP(reply_pkt):
                live_handshake = True
            reply_pkt = self.get_pkt(timeout=stop_time - time.time())

        if rst_returned:
            return "FIREWALL"
        elif live_handshake:
            return "LIVE"
        else:
            return "DEAD"

    # Format is
    # ([], [])
    # The first list is the list of IPs that have a hop
    # or none if none
    # The second list is T/F
    # if there is a RST back for that particular request
    def traceroute(self, target, hops):
        ips = []
        resets = []

        # send_port, _, synack_pkt = self.handshake()
        # if synack_pkt is None:
        #     return (ips, resets)

        # traceroute packets
        for i in range(hops + 1):
            # empty packet queue between hops
            while not self.packetQueue.empty():
                _ = self.get_pkt()

            send_port, _, synack_pkt = self.handshake()
            if synack_pkt is None:
                return (ips, resets)
            # print("seq sent", synack_pkt[IP][TCP].ack)
            # print("ack sent", synack_pkt[IP][TCP].seq + 1)
            for j in range(3):
                pkt = self.send_pkt(
                    payload=triggerfetch,
                    flags="PA",
                    ttl=i,
                    seq=synack_pkt[IP][TCP].ack,
                    ack=synack_pkt[IP][TCP].seq + 1,
                    sport=send_port,
                )

            # process packet queue
            reset_returned = False
            icmp_ip = None
            next_pkt = self.get_pkt(timeout=5)
            while next_pkt != None:
                if isTimeExceeded(next_pkt):
                    icmp_ip = next_pkt[IP].src
                    print('ICMP PACKET RECEIVED. IP: %s' % icmp_ip)
                else:
                    print('NON-ICMP PACKET RECEIVED. ACK: %s' % (next_pkt[IP][TCP].seq + 1))

                if isRST(next_pkt):
                    reset_returned = True
                    print('RST PACKET RECEIVED')

                next_pkt = self.get_pkt(timeout=5)

            ips.append(icmp_ip)
            resets.append(reset_returned)

            # if reset_returned:
            #     send_port, _, synack_pkt = self.handshake()
            # if synack_pkt is None:
            #     return (ips, resets)

        return (ips, resets)

    def handshake(self, timeout=30.0):
        stop_time = time.time() + timeout
        # handshake
        send_port = random.randrange(2000, 30000)
        send_seq = random.randint(1, 31313131)
        synack_pkt = None
        while (synack_pkt is None) and (time.time() < stop_time):
            syn_pkt = self.send_pkt(
                flags="S",
                seq=send_seq,
                sport=send_port,
            )
            synack_pkt = self.get_pkt(timeout=5)
            while synack_pkt != None and not (
                isSYNACK(synack_pkt) and
                synack_pkt[IP][TCP].ack == send_seq + 1
            ):
                synack_pkt = self.get_pkt()

        if synack_pkt is None:
            return None, None, None

        return send_port, send_seq, synack_pkt
