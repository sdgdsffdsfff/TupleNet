import threading
import os
import logging
import subprocess
import struct, socket
from pyDatalog import pyDatalog
from onexit import on_parent_exit
from tp_utils import pipe
from tp_utils.run_env import get_extra
from tuplesync import update_ovs_side,update_ovs_arp_ip_mac
from logicalview import LOGICAL_ENTITY_TYPE_LSP, LOGICAL_ENTITY_TYPE_CHASSIS, \
                        LOGICAL_ENTITY_TYPE_OVSPORT_CHASSIS

MAX_BUF_LEN = 10240

extra = get_extra()
logger = logging.getLogger(__name__)
entity_zoo = None

def int_to_ip(ip_int):
    return socket.inet_ntoa(struct.pack('I',socket.htonl(ip_int)))

def process_arp(arp_msg_seg):
    #TODO verify mac_addr, ip
    datapath = int(arp_msg_seg[1])
    mac_addr = arp_msg_seg[2]
    ip_int = int(arp_msg_seg[3])
    ip = int_to_ip(ip_int)
    if process_arp.arp_dict.has_key(ip_int) and \
       process_arp.arp_dict[ip_int] == mac_addr:
       logger.debug("skip duplicate mac_ip bind map[%s,%d(%s),%d]",
                    mac_addr, ip_int, ip, datapath)
       return
    else:
        logger.info("update arp mac_ip bind map[%s,%d(%s),%d]",
                    mac_addr, ip_int, ip, datapath)
        update_ovs_arp_ip_mac(mac_addr, ip_int)
        process_arp.arp_dict[ip_int] = mac_addr
process_arp.arp_dict = {}

def _parse_ofport2iface(ofport):
    if ofport == 0xffff:
        return '<INVALID_PORT>'
    elif ofport == 0:
        return '<UNK_PORT>'

    def _fn(ovsport_set, ofport):
        for ovsport in ovsport_set.values():
            if ovsport.ofport == ofport:
                return [ovsport]
        return []

    ovsport_array = entity_zoo.get_entity_by_fn(LOGICAL_ENTITY_TYPE_OVSPORT_CHASSIS,
                                                _fn, ofport)
    if len(ovsport_array) == 0:
        logger.info("cannot found ofport:%s's iface-id", ofport)
        return '<UNK_PORT>'
    return ovsport_array[0].iface_id

def process_trace(trace_msg_seg):
    table_id = int(trace_msg_seg[1])
    datapath_id = int(trace_msg_seg[2])
    cmd_id = int(trace_msg_seg[3]) >> 16
    src_port_id = int(trace_msg_seg[4])
    dst_port_id = int(trace_msg_seg[5])
    tun_src = int(trace_msg_seg[6])
    seq_n = int(trace_msg_seg[7])
    ofport = int(trace_msg_seg[8])
    logger.info('tracing packets, table_id:%d, datapath_id:%d, '
                'cmd_id:%d, src_port_id:%d, dst_port_id:%d, '
                'seq:%d, tun_src:%d, ofport:%d',
                table_id, datapath_id, cmd_id,
                src_port_id, dst_port_id, seq_n, tun_src, ofport)
    ttl = 30
    chassis_id = extra['system_id']
    key = "cmd_result/{}/{}/{}".format(cmd_id, seq_n, chassis_id)

    iface_id = _parse_ofport2iface(ofport)
    value = ("cmd_type=pkt_trace,table_id={},datapath_id={},"
             "src_port_id={},dst_port_id={},tun_src={},output_iface_id={}").format(
                table_id, datapath_id, src_port_id,
                dst_port_id, tun_src, iface_id)

    wmaster = extra['lm']
    wmaster.lease_communicate(key, value, ttl)

def process_unknow_dst(unknow_dst_msg_seg):
    datapath_id = int(unknow_dst_msg_seg[1])
    ip_int = int(unknow_dst_msg_seg[2])
    ip = int_to_ip(ip_int)
    logger.info("receive unknow packet: datapath:%d,dst_ip:%s", datapath_id, ip)

    # find all lsp by using ip
    # TODO figure out all or just one?
    def fn_lsp(lsp_portset, ip_int):
        array = []
        for _, lsp in lsp_portset.items():
            if lsp.ip_int == ip_int:
                array.append(lsp)
        return array

    def fn_chassis(chassis_set, chassis_uuid):
        return [chassis_set[chassis_uuid]] if chassis_set.has_key(chassis_uuid) else []

    lsp_array = entity_zoo.touch_entity(LOGICAL_ENTITY_TYPE_LSP, fn_lsp, ip_int)
    if len(lsp_array) == 0:
        return

    cnt = 0
    for lsp in lsp_array:
        if lsp.chassis is None:
            continue
        chassis_array = entity_zoo.touch_entity(LOGICAL_ENTITY_TYPE_CHASSIS,
                                                fn_chassis, lsp.chassis)
        cnt += len(chassis_array)

    if cnt != 0:
        update_ovs_side(entity_zoo)


def parse_pkt_controller_msg(msg):
    msg_array = msg.split(';')
    for cmd in msg_array:
        if cmd == '':
            continue
        try:
            segment = cmd.split(',')
            opcode = segment[0]
            if opcode == 'arp':
                process_arp(segment)
            elif opcode == 'trace':
                process_trace(segment)
            elif opcode == 'unknow_dst':
                process_unknow_dst(segment)
            else:
                logger.warning('unknow msg from pkt_controller,msg:%s', msg)
        except Exception as err:
            logger.exception('error in parsing pkt_controller msg, err:%s', err)
            continue

def run_pkt_controller_instance():
    env = os.environ.copy()
    if extra.has_key('log_dir'):
        env['TUPLENET_LOGDIR'] = extra['log_dir']
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = ['{}/pkt_controller/pkt_controller'.format(parent_dir)]
    try:
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE, env = env,
                                 preexec_fn=on_parent_exit('SIGTERM'))
        logger.info("the pkt_controller is running now")
    except Exception as err:
        logger.warning("cannot open %s, err:%s", cmd, err)


def monitor_pkt_controller_tunnel(ez, extra):
    # make global entity_zoo can be accessed
    global entity_zoo
    entity_zoo = ez
    try:
        pyDatalog.Logic(extra['logic'])
        run_pkt_controller_instance()
        fd = pipe.create_pkt_controller_tunnel()
        while True:
            msg = os.read(fd, MAX_BUF_LEN)
            if msg == '':
                logger.info('receive no msg, maybe pkt_controller is down')
                return
            parse_pkt_controller_msg(msg)
    except Exception as err:
        logger.warning("hit unknow error, exit monitoring pkt_controller:%s", err)


def start_monitor_pkt_controller_tunnel(entity_zoo, extra):
    t = threading.Thread(target = monitor_pkt_controller_tunnel,
                         args=(entity_zoo, extra))
    t.setDaemon(True)
    t.start()
    return t

