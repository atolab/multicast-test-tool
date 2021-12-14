import argparse
import ctypes
import ipaddress
import math
import random
import signal
import socket
import selectors
import statistics
import struct
import sys
import textwrap
import time

# The C equivalent of the test header is
# struct udpTestHeader_s {
#     int msgIndex;
#     int packetIndex;
#     double timestamp;
# };

UDPTESTER_HDRFORMAT = (
    "<2id"  # Little endian and consists of two ints and a double. See C struct above.
)
UDPTESTER_HDRSIZE = struct.calcsize(UDPTESTER_HDRFORMAT)
UDPTESTER_MIN_MSGSIZE = UDPTESTER_HDRSIZE
UDPTESTER_MIN_PKTSIZE = UDPTESTER_MIN_MSGSIZE


def ipAddressSanityCheck(address):
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return False
    return True


def ipAddressMulticastCheck(address):
    addressSplit = address.split(".")
    numbers = [int(element) for element in addressSplit if element.isdigit()]
    if numbers[0] < 224 or 239 < numbers[0]:
        # ip address must be in the range from 224.0.0.0 to 239.255.255.255
        return False
    else:
        return True  # OK


def multicastAddressCheck(address):
    if address == None:
        print("\nERROR: Missing multicast address.\n")
        return False
    elif not ipAddressSanityCheck(address):
        print("\nERROR: Provided multicast address is not a valid address.\n")
        return False
    elif not ipAddressMulticastCheck(address):
        print("\nERROR: Provided multicast address does not support multicast.\n")
        return False
    else:
        return True


def networkInterfaceCheck(interface):
    if interface == None:
        print("\nERROR: Missing network interface address.\n")
        return False
    elif not ipAddressSanityCheck(interface):
        print("\nERROR: Provided network interface is not a valid address.\n")
        return False
    else:
        return True


def UDPTESTER_CEILTO_MIN_PKTSIZE(size):
    if size < UDPTESTER_MIN_PKTSIZE:
        return UDPTESTER_MIN_PKTSIZE
    else:
        return size


def progressBar(it, prefix="", size=60, file=sys.stdout):
    count = len(it)

    def show(j):
        x = int(size * j / count)
        file.write("%s[%s%s] %i/%i\r" % (prefix, "#" * x, "." * (size - x), j, count))
        file.flush()

    show(0)
    for i, item in enumerate(it):
        yield item
        show(i + 1)
    file.write("\n")
    file.flush()


class socketWaitset:
    def __init__(self, sock):
        self.sel = selectors.DefaultSelector()
        self.sel.register(sock, selectors.EVENT_READ)

    def close(self):
        self.sel.close()

    # The wait is used before socket.recvfrom() in order to see when the socket has data.
    # In Windows, both socket.recvfrom() and selectors.select() are blocking calls that
    # can't be interrupted by ctrl c. As a workaround, multiple mini waits are used
    # to have reasonable response time when pressing ctrl c to stop the application.
    def wait(self, timeout):
        miniTimeout = 0.3
        max_attempts = math.ceil(timeout / miniTimeout)
        for attempt in range(0, max_attempts):
            if self.sel.select(miniTimeout):
                return True  # Data available
        return False  # Timeout


class udpMetricsReportItem:
    def __init__(
        self,
        percentile=None,
        valueCount=None,
        totalValueCount=None,
        minimum=None,
        average=None,
        maximum=None,
        deviation=None,
    ):
        self.percentile: float = percentile or 0.0
        self.valueCount: int = valueCount or 0
        self.totalValueCount: int = totalValueCount or 0
        self.minimum: float = minimum or 0
        self.average: float = average or 0
        self.maximum: float = maximum or 0
        self.deviation: float = deviation or 0

    def __str__(self):
        return (
            f"{self.percentile:5.1f} % : cnt= {self.valueCount}/{self.totalValueCount},"
            f" latency [usec]: min= {self.minimum:.0f},"
            f" avg= {self.average:.0f}, max= {self.maximum:.0f}, deviation= {self.deviation:.2f}"
        )


class udpMetrics:
    def __init__(self, max_number_of_values, values=None):
        self.max_number_of_values: int = max_number_of_values
        self.values: list = values or []

    def append(self, value):
        if len(self.values) < self.max_number_of_values:
            self.values.append(value)

    def report(self, percentile):
        # ensure at least one value
        count = int(len(self.values) * percentile / 100.0)

        if count < 1:
            return udpMetricsReportItem()

        values = self.values[:count]

        return udpMetricsReportItem(
            percentile=percentile,
            valueCount=len(values),
            totalValueCount=len(self.values),
            minimum=values[0],
            average=statistics.mean(values),
            maximum=values[-1],
            deviation=statistics.stdev(values) if len(values) > 1 else math.nan,
        )

    def reports(self, percentiles):
        self.values.sort()
        return [self.report(percentile) for percentile in percentiles]


def transmitter(parser):
    print("I am the transmitter")
    DEFAULT_MSGSIZE = 100
    DEFAULT_FREQUENCY = 50
    DEFAULT_TOTNOFMSGS = 1000
    DEFAULT_PORTNR = 10350
    DEFAULT_PACKETSIZE = 1300
    DEFAULT_LOSSINESS = 0
    DEFAULT_MULTI_TTL = 64

    messageSize = DEFAULT_MSGSIZE
    totNofMsgs = DEFAULT_TOTNOFMSGS
    frequency = DEFAULT_FREQUENCY
    portNr = DEFAULT_PORTNR
    packetSize = DEFAULT_PACKETSIZE
    lossiness = DEFAULT_LOSSINESS
    multiTTL = DEFAULT_MULTI_TTL

    args, args_remaining = parser.parse_known_args()

    # Multicast address is required
    if not multicastAddressCheck(args.address):
        parser.print_help()
        sys.exit(1)
    else:
        address = args.address

    # Network interface address is required
    if not networkInterfaceCheck(args.interface):
        parser.print_help()
        sys.exit(1)
    else:
        interface = args.interface

    # Optional arguments
    if args.port != None:
        portNr = args.port
    if args.messagesize != None:
        messageSize = args.messagesize
    if args.totalcount != None:
        totNofMsgs = args.totalcount
    if args.packetsize != None:
        packetSize = args.packetsize
    if args.frequency != None and args.frequency > 0:
        frequency = args.frequency
    if args.lossiness != None:
        lossiness = args.lossiness

    print(
        f"""
    address is set to       {address}
    interface is set to     {interface}
    port is set to          {portNr}
    totalcount is set to    {totNofMsgs}
    messagesize is set to   {messageSize} bytes
    packetsize is set to    {packetSize} bytes
    frequency is set to     {frequency} Hz
    lossiness is set to     {lossiness}%"""
    )

    # Set the socket options for multicast transmitter
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ttl = struct.pack("<b", multiTTL)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    try:
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)
        )
    except:
        print(
            f"\nERROR: Failed to set interface to {interface}. Did you provide the correct interface?\n"
        )
        parser.print_help()
        sys.exit(1)
    multicast_group = (address, portNr)

    sleepTime = 1.0 / frequency
    progress = progressBar(totNofMsgs)
    print(f"  Sending {totNofMsgs} messages now...")
    for i in progressBar(range(0, totNofMsgs), "  Progress: ", 20):
        msgIndex = i
        packetIndex = 0
        timestamp = time.time()
        remainingSize = messageSize

        # Loop until the full msgSize has been sent.
        while remainingSize > 0:
            if remainingSize > packetSize:
                bufSize = UDPTESTER_CEILTO_MIN_PKTSIZE(packetSize)
            else:
                bufSize = UDPTESTER_CEILTO_MIN_PKTSIZE(remainingSize)
            # In case of lossiness, I randomly skip sending the packet.
            if not (lossiness and (random.randint(0, 100) < lossiness)):
                buffer = ctypes.create_string_buffer(bufSize)
                struct.pack_into(
                    UDPTESTER_HDRFORMAT, buffer, 0, msgIndex, packetIndex, timestamp
                )

                # The buffer contains my data in C struct format, and is sent using the socket.
                sock.sendto(buffer, multicast_group)
            remainingSize -= bufSize
            packetIndex += 1
        time.sleep(sleepTime)
    sock.close()


def receiver(parser):
    print("I am the receiver")
    DEFAULT_PORTNR = 10350
    DEFAULT_MESSAGESIZE = 100
    DEFAULT_EXPECTEDCOUNT = 1000
    DEFAULT_REPORTINTERVAL = 100
    DEFAULT_PACKETSIZE = 1300
    DEFAULT_RCVBUFSIZE = 120000

    RECEIVE_TIMEOUT_SEC = 10
    RECEIVE_TIMEOUT_SEC_INITIAL = 100

    portNr = DEFAULT_PORTNR
    messageSize = DEFAULT_MESSAGESIZE
    expectedCount = DEFAULT_EXPECTEDCOUNT
    reportInterval = DEFAULT_REPORTINTERVAL
    packetSize = DEFAULT_PACKETSIZE
    rcvBufSize = DEFAULT_RCVBUFSIZE

    args, args_remaining = parser.parse_known_args()

    # Multicast address is required
    if not multicastAddressCheck(args.address):
        parser.print_help()
        sys.exit(1)
    else:
        joinAddressString = args.address

    # Network interface address is required
    if not networkInterfaceCheck(args.interface):
        parser.print_help()
        sys.exit(1)
    else:
        interface = args.interface

    if args.port != None:
        portNr = args.port
    if args.messagesize != None:
        messageSize = args.messagesize
    if args.totalcount != None:
        expectedCount = args.totalcount
    if args.packetsize != None:
        packetSize = args.packetsize
    if args.reportinterval != None:
        reportInterval = args.reportinterval
    if args.receivebuffer != None:
        rcvBufSize = args.receivebuffer

    print(
        f"""
    joinaddress is set to    {joinAddressString}
    interface is set to      {interface}
    port is set to           {portNr}
    totalcount is set to     {expectedCount}
    messagesize is set to    {messageSize} bytes
    packetsize is set to     {packetSize} bytes
    receivebuffer is set to  {rcvBufSize} bytes
    reportInterval is set to {reportInterval}"""
    )

    # Set the socket options for multicast receiver
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvBufSize)
    sock.bind(("", portNr))
    multicast_group = socket.inet_aton(joinAddressString)
    networkInterface = socket.inet_aton(interface)
    mreq = struct.pack("4s4s", multicast_group, networkInterface)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except:
        print(
            f"\nERROR: Failed to set interface to {interface}. Did you provide the correct interface?\n"
        )
        parser.print_help()
        sys.exit(1)
    sock.setblocking(False)
    waitset = socketWaitset(sock)

    # Metrics about the received data
    metrics = udpMetrics(reportInterval)
    percentiles = [100.0, 99.9, 99.0, 90.0]
    expectedMsgIndex = 0
    expectedPacketIndex = 0
    packetsPerMessage = math.ceil(messageSize / packetSize)
    reportCount = reportInterval * packetsPerMessage
    nextAnalyseIndex = reportInterval
    totalPacketsExpected = packetsPerMessage * expectedCount
    totalPackets = 0
    totalMsgs = 0
    subTotalMsgs = 0
    msgIncomplete = 0
    tallysheet = [[0] * packetsPerMessage for j in range(0, expectedCount)]
    duplicatePackets = 0
    receiveTimeOut = RECEIVE_TIMEOUT_SEC_INITIAL

    print(f"  Waiting for {expectedCount} messages now...")
    while expectedMsgIndex < expectedCount:
        # The buffer contains transmitter's data in C struct format, and is received using the socket.
        if not waitset.wait(receiveTimeOut):
            print(
                f"WARNING: Timed out after {receiveTimeOut} seconds whilst waiting for packets."
            )
            break
        bytedata, sourceAddress = sock.recvfrom(packetSize)
        (msgIndex, packetIndex, timestamp) = struct.unpack(
            UDPTESTER_HDRFORMAT, bytedata[:UDPTESTER_HDRSIZE]
        )

        # Check and count received packets.
        if (0 <= msgIndex and msgIndex < expectedCount) and (
            0 <= packetIndex and packetIndex < packetsPerMessage
        ):
            tallysheet[msgIndex][packetIndex] += 1
        if tallysheet[msgIndex][packetIndex] > 1:
            duplicatePackets += 1
        if (msgIndex != expectedMsgIndex) or (packetIndex != expectedPacketIndex):
            print(
                f"Expected msgIndex {expectedMsgIndex} and packetIndex {expectedPacketIndex}, "
                f"received msgIndex {msgIndex} and packetIndex {packetIndex} with count {tallysheet[ msgIndex ][ packetIndex ]}"
            )
            expectedMsgIndex = msgIndex
            msgIncomplete = packetIndex != 0
        if packetIndex == (packetsPerMessage - 1):
            expectedMsgIndex += 1
            expectedPacketIndex = 0
            if msgIncomplete:
                msgIncomplete = 0
            else:
                totalMsgs += 1
                subTotalMsgs += 1
                metrics.append(
                    (time.time() - timestamp) * 1e6
                )  # Latency in microseconds
        else:
            expectedPacketIndex = packetIndex + 1
        totalPackets += 1
        reportCount -= 1

        # Report metrics
        if reportCount == 0:
            print(
                f"received {totalPackets} packets from {sourceAddress[0]}, expecting {totalPacketsExpected} in total"
            )
            reportCount = reportInterval * packetsPerMessage

        if expectedMsgIndex >= nextAnalyseIndex:
            print(f"Expecting message with index {expectedMsgIndex}:")
            print(f"    {totalMsgs:5d} complete messages so far.")
            print(f"    {subTotalMsgs:5d} complete messages since the last report.")

            for reportItem in metrics.reports(percentiles):
                print(f"    {reportItem}")

            metrics = udpMetrics(reportInterval)
            nextAnalyseIndex = (
                expectedMsgIndex + reportInterval - expectedMsgIndex % reportInterval
            )
            subTotalMsgs = 0

        if receiveTimeOut != RECEIVE_TIMEOUT_SEC:
            receiveTimeOut = RECEIVE_TIMEOUT_SEC
    # end while
    waitset.close()
    sock.close()
    print("Done")
    lost = 100.0 * float((totalPacketsExpected - totalPackets) / totalPacketsExpected)
    print(
        f"Received {totalPackets} packets out of {totalPacketsExpected}, lost {lost:.1f}%"
    )
    lost = 100.0 * float((expectedCount - totalMsgs) / expectedCount)
    print(
        f"Received {totalMsgs} complete messages out of {expectedCount}, lost {lost:.1f}%"
    )
    print(f"Received {duplicatePackets} duplicate packets")


def print_help_subparsers(parser):
    # retrieve subparsers from parser
    subparsers_actions = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    # there will probably only be one subparser_action,
    # but better save than sorry
    for subparsers_action in subparsers_actions:
        # get all subparsers and print help
        for choice, subparser in subparsers_action.choices.items():
            print("\n{}:".format(choice))
            print(textwrap.indent(f"""{subparser.format_help()}""", "    "))
    parser.exit()


class _HelpAction(argparse._HelpAction):
    def __call__(self, parser, namespace, values, option_string=None):
        print_help_subparsers(parser)


def create_parser():
    parser = argparse.ArgumentParser(
        description="Test multicast traffic", add_help=False
    )
    parser.add_argument(
        "-h", "--help", action=_HelpAction, help="show this help message and exit"
    )

    parser_common = argparse.ArgumentParser(
        description="Capture common arguments", add_help=False
    )
    parser_common.add_argument(
        "-a",
        "--address",
        help="Mandatory argument: The ip address to use. Multicast addresses range from 224.0.0.0 to 239.255.255.255",
        type=str,
    )
    parser_common.add_argument(
        "-i",
        "--interface",
        help="Mandatory argument: This is the network interface address to use",
        type=str,
    )
    parser_common.add_argument("-p", "--port", help="The port number to use.", type=int)
    parser_common.add_argument(
        "-t", "--totalcount", help="Number of messages", type=int
    )
    parser_common.add_argument(
        "-m",
        "--messagesize",
        help="Bytes per message. A message may consist of multiple packets.",
        type=int,
    )
    parser_common.add_argument(
        "-s", "--packetsize", help="Bytes per packet sent", type=int
    )

    subparsers = parser.add_subparsers(dest="role", title="subcommands")

    # transmitter specific args
    epilogStringTransmitter = """
Example:
    python3 udpTester.py transmitter -a 239.0.0.1 -i 192.168.2.33 -t 200 -m 450 -s 150 -f 60
    """
    parser_transmitter = subparsers.add_parser(
        "transmitter",
        description="Send messages via multicast. If the receiver receives all of them, the network has passed the test.",
        help="Select the transmitter role",
        parents=[parser_common],
        epilog=epilogStringTransmitter,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser_transmitter.add_argument(
        "-f",
        "--frequency",
        help="transmitter option: Frequency of sending messages in unit Hz.",
        type=float,
    )
    parser_transmitter.add_argument(
        "-l",
        "--lossiness",
        help="transmitter option: Randomly skip sending a packet, chance in unit %%.",
        type=int,
    )

    # receiver specific args
    epilogStringReceiver = """
Example:
    python3 udpTester.py receiver -a 239.0.0.1 -i 192.168.2.33 -t 200 -m 450 -s 150 -b 200000
    """
    description_string = """
Receive multicast messages. If I receive all messages from the transmitter, the network has passed the test.
The results will also show latency values in microseconds, calculated as current_time - source_timestamp. These values
are not reliable when transmitter's and receiver's clocks are not precisely synchronized, and may be negative when the 
receiver's clock is trailing the transmitter's clock.
    """
    parser_receiver = subparsers.add_parser(
        "receiver",
        description=description_string,
        help="Select the receiver role",
        parents=[parser_common],
        epilog=epilogStringReceiver,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser_receiver.add_argument(
        "-b",
        "--receivebuffer",
        help="receiver option: receivebuffer size in bytes.",
        type=int,
    )
    parser_receiver.add_argument(
        "-r",
        "--reportinterval",
        help="receiver option: Number of messages per report.",
        type=int,
    )

    return (parser, parser_receiver, parser_transmitter)


def activate_signal_handler():
    def signalHandler(signum, frame):
        print(" Ctrl c was pressed. Exiting...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signalHandler)


if __name__ == "__main__":
    activate_signal_handler()
    (parser, parser_receiver, parser_transmitter) = create_parser()

    # Start the transmitter or receiver
    args = parser.parse_args()
    if args.role == "transmitter":
        transmitter(parser_transmitter)
    elif args.role == "receiver":
        receiver(parser_receiver)
    else:
        print_help_subparsers(parser)
        sys.exit(1)
