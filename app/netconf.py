"""Set per-interface IPv4 settings (the ones in /proc/sys/net/ipv4/conf/<if>/)
over netlink.

Inside a container /proc/sys is read-only, but the same settings can be
changed with an RTM_SETLINK message, which only needs CAP_NET_ADMIN - the
permission Stowaway already has for creating the macvlan helper.
"""
import os
import socket
import struct

RTM_SETLINK = 19
NLM_F_REQUEST, NLM_F_ACK = 0x1, 0x4
NLMSG_ERROR = 2
NLA_F_NESTED = 0x8000
IFLA_AF_SPEC = 26
IFLA_INET_CONF = 1

# from include/uapi/linux/ip.h (enum IPV4_DEVCONF_*)
ARP_ANNOUNCE = 18
ARP_IGNORE = 19


def _nla(kind: int, payload: bytes) -> bytes:
    length = 4 + len(payload)
    return struct.pack("HH", length, kind) + payload + b"\0" * ((4 - length % 4) % 4)


def set_ipv4_conf(ifname: str, values: dict[int, int]):
    index = socket.if_nametoindex(ifname)
    conf = b"".join(_nla(k, struct.pack("I", v)) for k, v in values.items())
    body = struct.pack("BxHiII", socket.AF_UNSPEC, 0, index, 0, 0) + _nla(
        IFLA_AF_SPEC | NLA_F_NESTED,
        _nla(socket.AF_INET | NLA_F_NESTED, _nla(IFLA_INET_CONF | NLA_F_NESTED, conf)))
    msg = struct.pack("IHHII", 16 + len(body), RTM_SETLINK, NLM_F_REQUEST | NLM_F_ACK, 1, 0) + body
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as s:
        s.bind((0, 0))
        s.send(msg)
        resp = s.recv(4096)
    if struct.unpack("H", resp[4:6])[0] == NLMSG_ERROR:
        err = struct.unpack("i", resp[16:20])[0]
        if err:
            raise OSError(-err, f"setting {ifname} options: {os.strerror(-err)}")


def quiet_arp(ifname: str):
    """Only answer ARP for this interface's own address, and only announce it.

    Without this, Linux answers "who has <server IP>?" on the helper interface
    too, with the helper's MAC. The network then sees two MACs for the server,
    which security software reports as ARP spoofing.
    """
    set_ipv4_conf(ifname, {ARP_IGNORE: 1, ARP_ANNOUNCE: 2})
