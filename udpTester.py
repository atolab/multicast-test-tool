import argparse
import ctypes
import dataclasses
import math
import random
import signal
import socket
import selectors
import statistics
import struct
import sys
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


def ipaddressSanitycheck(address):
    addressSplit = address.split(".")
    numbers = [int(element) for element in addressSplit]

    if len(addressSplit) != 4:
        raise ValueError("ip address must consist of four numbers separated by a dot.")

    for number in addressSplit:
        if int(number) < 0 or 255 < int(number):
            raise ValueError(
                "Not all numbers in ip address are in the range from 0 to 255."
            )


def ipaddressMulticastcheck(address):
    addressSplit = address.split(".")
    numbers = [int(element) for element in addressSplit]

    if numbers[0] < 224 or 239 < numbers[0]:
        raise ValueError(
            "ip address is not in the range from 224.0.0.0 to 239.255.255.255"
        )


def UDPTESTER_CEILTO_MIN_PKTSIZE(size):
    if size < UDPTESTER_MIN_PKTSIZE:
        return UDPTESTER_MIN_PKTSIZE
    else:
        return size


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
                return True
        raise TimeoutError("socketWaitset.wait() timed out")


@dataclasses.dataclass
class udpMetricsReportItem:
    percentile: float = 0.0
    sampleCount: int = 0
    totalSampleCount: int = 0
    minimum: float = 0
    average: float = 0
    maximum: float = 0
    deviation: float = 0

    def __str__(self):
        return (
            f"{self.percentile:5.1f} % : cnt= {self.sampleCount}/{self.totalSampleCount}, min= {self.minimum:.0f},"
            f" avg= {self.average:.0f}, max= {self.maximum:.0f}, dev= {self.deviation:.2f}"
        )


@dataclasses.dataclass
class udpMetrics:
    max_number_of_samples: int
    samples: list = dataclasses.field(default_factory=list)

    def append(self, sample):
        if len(self.samples) < self.max_number_of_samples:
            self.samples.append(sample)

    def report(self, percentile):
        # ensure at least one sample
        if not self.samples:
            return udpMetricsReportItem()

        count = 1 + math.floor(len(self.samples) * percentile / 100.0 - 0.999)
        samples = self.samples[:count]

        return udpMetricsReportItem(
            percentile=percentile,
            sampleCount=len(samples),
            totalSampleCount=len(self.samples),
            minimum=samples[0],
            average=statistics.mean(samples),
            maximum=samples[-1],
            deviation=statistics.stdev(samples) if len(samples) > 1 else math.inf,
        )

    def reports(self, percentiles):
        self.samples.sort()
        return [self.report(percentile) for percentile in percentiles]


def transmitter(args):
    print("I am the transmitter")
    DEFAULT_MSGSIZE = 100
    DEFAULT_SLEEPTIME = 20
    DEFAULT_TOTNOFMSGS = 1000
    DEFAULT_PORTNR = 10350
    DEFAULT_PACKETSIZE = 1300
    DEFAULT_LOSSINESS = 0
    DEFAULT_MULTI_TTL = 64

    messageSize = DEFAULT_MSGSIZE
    totNofMsgs = DEFAULT_TOTNOFMSGS
    sleepTime = DEFAULT_SLEEPTIME
    portNr = DEFAULT_PORTNR
    address = args.address  # Required argument
    interface = args.outgoing  # Required argument
    packetSize = DEFAULT_PACKETSIZE
    lossiness = DEFAULT_LOSSINESS
    multiTTL = DEFAULT_MULTI_TTL

    if args.port != None:
        portNr = args.port
    if args.messagesize != None:
        messageSize = args.messagesize
    if args.totalcount != None:
        totNofMsgs = args.totalcount
    if args.packetsize != None:
        packetSize = args.packetsize
    if args.interval != None:
        sleepTime = args.interval
    if args.outgoing != None:
        ipaddressSanitycheck(args.outgoing)
        interface = args.outgoing
    if args.lossiness != None:
        lossiness = args.lossiness

    print(
        f"""
    address is set to       {address}
    messagesize is set to   {messageSize} bytes
    interval is set to      {sleepTime} milliseconds
    totalcount is set to    {totNofMsgs}
    port is set to          {portNr}
    packetsize is set to    {packetSize} bytes
    outgoing is set to      {interface}
    lossiness is set to     {lossiness}%"""
    )

    # Set the socket options for multicast transmitter
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ttl = struct.pack("<b", multiTTL)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)
    )
    multicast_group = (address, portNr)

    print(f"  Sending {totNofMsgs} messages now...")

    for i in range(0, totNofMsgs):
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
        time.sleep(sleepTime * 1e-3)
    sock.close()


def receiver(args):
    print("I am the receiver")
    DEFAULT_PORTNR = 10350
    DEFAULT_MESSAGESIZE = 100
    DEFAULT_EXPECTEDCOUNT = 1000
    DEFAULT_REPORTINTERVAL = 100
    DEFAULT_PACKETSIZE = 1300
    DEFAULT_RCVBUFSIZE = 120000
    DEFAULT_QUIET = 0

    RECEIVE_TIMEOUT_SEC = 10
    RECEIVE_TIMEOUT_SEC_INITIAL = 100

    portNr = DEFAULT_PORTNR
    messageSize = DEFAULT_MESSAGESIZE
    expectedCount = DEFAULT_EXPECTEDCOUNT
    reportInterval = DEFAULT_REPORTINTERVAL
    packetSize = DEFAULT_PACKETSIZE
    rcvBufSize = DEFAULT_RCVBUFSIZE
    joinAddressString = args.address  # Required argument
    quiet = DEFAULT_QUIET

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
    if args.quiet != None:
        quiet = args.quiet

    print(
        f"""
    messagesize is set to   {messageSize} bytes
    totalcount is set to    {expectedCount}
    port is set to          {portNr}
    packetsize is set to    {packetSize} bytes
    receivebuffer is set to {rcvBufSize} bytes
    joinaddress is set to   {joinAddressString}
    quiet is set to         {quiet}"""
    )

    # Set the socket options for multicast receiver
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvBufSize)
    sock.bind(("", portNr))
    multicast_group = socket.inet_aton(joinAddressString)
    mreq = struct.pack("4sL", multicast_group, socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
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
    receiveTimeOut = RECEIVE_TIMEOUT_SEC_INITIAL

    print(f"  Waiting for {expectedCount} messages now...")
    while expectedMsgIndex < expectedCount:
        # The buffer contains transmitter's data in C struct format, and is received using the socket.
        waitset.wait(receiveTimeOut)
        bytedata, sourceAddress = sock.recvfrom(packetSize)
        (msgIndex, packetIndex, timestamp) = struct.unpack(
            UDPTESTER_HDRFORMAT, bytedata[:UDPTESTER_HDRSIZE]
        )

        # Check and count received packets.
        if (msgIndex != expectedMsgIndex) or (packetIndex != expectedPacketIndex):
            if not quiet:
                print(
                    f"Expected msgIndex {expectedMsgIndex} and packetIndex {expectedPacketIndex}, "
                    f"received msgIndex {msgIndex} and packetIndex {packetIndex}"
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
                f"received {totalPackets} packets, expecting {totalPacketsExpected} in total"
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


def create_parser():
    # shared args
    usageString = """
    Running the transmitter:
        udpTester.py transmitter address outgoing [-h] [-p PORT] [-m MESSAGESIZE] [-t TOTALCOUNT] [-s PACKETSIZE] [-i INTERVAL] [-l LOSSINESS]

    Running the receiver:
        udpTester.py receiver address [-h] [-p PORT] [-m MESSAGESIZE] [-t TOTALCOUNT] [-s PACKETSIZE] [-r REPORTINTERVAL] [-b RECEIVEBUFFER] [-q QUIET]

    Note that outgoing is a mandatory argument for the transmitter. It is the network interface address used for outgoing packets.
    """
    parser = argparse.ArgumentParser(
        usage=usageString, description="Test multicast traffic"
    )
    parser.add_argument(
        "role", help="The role selected", type=str, choices=["transmitter", "receiver"]
    )
    parser.add_argument(
        "address",
        help="The ip address to use. Multicast addresses range from 224.0.0.0 to 239.255.255.255",
        type=str,
    )
    parser.add_argument("-p", "--port", help="The port number to use.", type=int)
    parser.add_argument(
        "-m",
        "--messagesize",
        help="Bytes per message. A message may consist of multiple packets.",
        type=int,
    )
    parser.add_argument("-t", "--totalcount", help="Number of messages", type=int)
    parser.add_argument("-s", "--packetsize", help="Bytes per packet sent", type=int)

    # transmitter specific args
    if "transmitter" in sys.argv:
        parser.add_argument(
            "outgoing",
            help="transmitter mandatory argument: This is the network interface address to use",
            type=str,
        )
    parser.add_argument(
        "-i",
        "--interval",
        help="transmitter option: Interval between messages in unit milliseconds.",
        type=int,
    )
    parser.add_argument(
        "-l",
        "--lossiness",
        help="transmitter option: Randomly skip sending a packet.",
        type=int,
    )

    # receiver specific args
    parser.add_argument(
        "-r",
        "--reportinterval",
        help="receiver option: Number of messages per report.",
        type=int,
    )
    parser.add_argument(
        "-b",
        "--receivebuffer",
        help="receiver option: receivebuffer size in bytes.",
        type=int,
    )
    parser.add_argument(
        "-q", "--quiet", help="receiver option: Suppress some prints.", type=int
    )

    return parser


def activate_signal_handler():
    # signalhandler
    def signalHandler(signum, frame):
        print(" Ctrl c was pressed. Exiting...")
        exit(1)

    signal.signal(signal.SIGINT, signalHandler)


if __name__ == "__main__":
    activate_signal_handler()
    parser = create_parser()

    # Start the transmitter or receiver
    args = parser.parse_args()
    if not args:
        parser.print_help()
        sys.exit(1)

    ipaddressSanitycheck(args.address)
    ipaddressMulticastcheck(args.address)

    if args.role == "transmitter":
        transmitter(args)
    elif args.role == "receiver":
        receiver(args)
