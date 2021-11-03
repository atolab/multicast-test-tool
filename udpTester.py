import argparse
import sys
import signal
import socket
import selectors
import struct
import ctypes
import time
import random
import math
import statistics

# The C equivalent of the test header is
# struct udpTestHeader_s {
#     int msgIndex;
#     int packetIndex;
#     double timestamp;
# };

UDPTESTER_HDRFORMAT   = "<2id" # Little endian and consists of two ints and a double. See C struct above.
UDPTESTER_HDRSIZE     = struct.calcsize(UDPTESTER_HDRFORMAT)
UDPTESTER_MIN_MSGSIZE = UDPTESTER_HDRSIZE
UDPTESTER_MIN_PKTSIZE = UDPTESTER_MIN_MSGSIZE

def ipaddressSanitycheck(address):
    addressSplit = address.split('.')
    numbers = [int(element) for element in addressSplit]
    if( len(addressSplit) != 4 ):
        raise ValueError("ip address must consist of four numbers separated by a dot.")
    for number in addressSplit:
        if( int(number) < 0 or 255 < int(number) ):
            raise ValueError("Not all numbers in ip address are in the range from 0 to 255.")

def ipaddressMulticastcheck(address):
    addressSplit = address.split('.')
    numbers = [int(element) for element in addressSplit]
    if( numbers[0] < 224 or 239 < numbers[0] ):
        raise ValueError("ip address is not in the range from 224.0.0.0 to 239.255.255.255")

def UDPTESTER_CEILTO_MIN_PKTSIZE(size):
    if( size < UDPTESTER_MIN_PKTSIZE ):
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
        max_attempts = math.ceil( timeout / miniTimeout )
        for attempt in range(0, max_attempts):
            events = self.sel.select(miniTimeout)
            if( events != [] ):
                return True
        raise TimeoutError("socketWaitset.wait() timed out")

class udpMetricsReportItem:
    percentile = 0
    sampleCount = 0
    minimum = 0
    average = 0
    maximum = 0
    deviation = 0
    def print(self, prefix):
        print("{}{:5.1f} % : cnt= {}, min= {:.0f}, avg= {:.0f}, max= {:.0f}, dev= {:.2f}".format(
            prefix,
            self.percentile,
            self.sampleCount,
            self.minimum,
            self.average,
            self.maximum,
            self.deviation))

class udpMetrics:
    sampleCount = 0
    maxCount = 0
    values = []
    def __init__(self, maxNofSamples):
        self.reset()
        self.maxCount = maxNofSamples

    def reset(self):
        self.sampleCount = 0
        self.values = []

    def addValue(self, value):
        assert(self.sampleCount < self.maxCount)
        if(self.sampleCount < self.maxCount):
            self.values.append(value)
            self.sampleCount += 1

    def calculateReport(self, percentile):
        if( self.sampleCount <= 0 ):
            return udpMetricsReportItem()
        else:
            reportItem = udpMetricsReportItem()
            range = int((self.sampleCount * percentile)/100)
            reportItem.percentile  = percentile
            reportItem.sampleCount = self.sampleCount
            reportItem.minimum     = self.values[0]
            if( len(self.values[:range]) <= 0 ):
                reportItem.average = math.nan
            else:
                reportItem.average = statistics.mean(self.values[:range])
            reportItem.maximum     = self.values[range-1]
            if( len(self.values[:range]) <= 1 ):
                reportItem.deviation = math.nan
            else:
                reportItem.deviation = statistics.stdev(self.values[:range])
            return reportItem

    def analyse(self, percentiles):
        self.values.sort()
        udpMetricsReport = []
        for percentile in percentiles:
            udpMetricsReport.append(self.calculateReport(percentile))
        return udpMetricsReport

def transmitter(args):
    print("I am the transmitter")
    DEFAULT_MSGSIZE    =       100
    DEFAULT_SLEEPTIME  =        20
    DEFAULT_TOTNOFMSGS =      1000
    DEFAULT_PORTNR     =     10350
    DEFAULT_PACKETSIZE =      1300
    DEFAULT_LOSSINESS  =         0
    DEFAULT_MULTI_TTL  =        64

    messageSize = DEFAULT_MSGSIZE
    totNofMsgs = DEFAULT_TOTNOFMSGS
    sleepTime = DEFAULT_SLEEPTIME
    portNr = DEFAULT_PORTNR
    address = args.address # Required argument
    interface = args.outgoing # Required argument
    packetSize = DEFAULT_PACKETSIZE
    lossiness = DEFAULT_LOSSINESS
    multiTTL = DEFAULT_MULTI_TTL

    if( args.port != None ):
        portNr = args.port
    if( args.messagesize != None ):
        messageSize = args.messagesize
    if( args.totalcount != None ):
        totNofMsgs = args.totalcount
    if( args.packetsize != None ):
        packetSize = args.packetsize
    if( args.interval != None ):
        sleepTime = args.interval
    if( args.outgoing != None ):
        ipaddressSanitycheck(args.outgoing)
        interface = args.outgoing
    if( args.lossiness != None ):
        lossiness = args.lossiness

    print("  address is set to     ", address)
    print("  messagesize is set to  {} bytes".format(messageSize))
    print("  interval is set to     {} milliseconds".format(sleepTime))
    print("  totalcount is set to  ", totNofMsgs)
    print("  port is set to        ", portNr)
    print("  packetsize is set to   {} bytes".format(packetSize))
    print("  outgoing is set to    ", interface)
    print("  lossiness is set to    {} %".format(lossiness))

    # Set the socket options for multicast transmitter
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ttl = struct.pack('<b', multiTTL)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
    multicast_group = (address, portNr)

    print("  Sending {} messages now...".format(totNofMsgs))
    for i in range(0, totNofMsgs):
        msgIndex = i
        packetIndex = 0
        timestamp = time.time()
        remainingSize = messageSize

        # Loop until the full msgSize has been sent.
        while( remainingSize > 0 ):
            if( remainingSize > packetSize ):
                bufSize = UDPTESTER_CEILTO_MIN_PKTSIZE(packetSize)
            else:
                bufSize = UDPTESTER_CEILTO_MIN_PKTSIZE(remainingSize)
            # In case of lossiness, I randomly skip sending the packet.
            if( not (lossiness and (random.randint(0, 100) < lossiness) ) ):
                buffer = ctypes.create_string_buffer(bufSize)
                struct.pack_into(UDPTESTER_HDRFORMAT, buffer, 0, msgIndex, packetIndex, timestamp)

                # The buffer contains my data in C struct format, and is sent using the socket.
                sock.sendto(buffer, multicast_group)
            remainingSize -= bufSize
            packetIndex   += 1
        time.sleep(sleepTime*1e-3)
    sock.close()

def receiver(args):
    print("I am the receiver")
    DEFAULT_PORTNR             =  10350
    DEFAULT_MESSAGESIZE        =    100
    DEFAULT_EXPECTEDCOUNT      =   1000
    DEFAULT_REPORTINTERVAL     =    100
    DEFAULT_PACKETSIZE         =   1300
    DEFAULT_RCVBUFSIZE         = 120000
    DEFAULT_QUIET              =      0

    RECEIVE_TIMEOUT_SEC         =   10
    RECEIVE_TIMEOUT_SEC_INITIAL =  100

    portNr = DEFAULT_PORTNR
    messageSize = DEFAULT_MESSAGESIZE
    expectedCount = DEFAULT_EXPECTEDCOUNT
    reportInterval = DEFAULT_REPORTINTERVAL
    packetSize = DEFAULT_PACKETSIZE
    rcvBufSize = DEFAULT_RCVBUFSIZE
    joinAddressString = args.address # Required argument
    quiet = DEFAULT_QUIET

    if( args.port != None ):
        portNr = args.port
    if( args.messagesize != None ):
        messageSize = args.messagesize
    if( args.totalcount != None ):
        expectedCount = args.totalcount
    if( args.packetsize != None ):
        packetSize = args.packetsize
    if( args.reportinterval != None ):
        reportInterval = args.reportinterval
    if( args.receivebuffer != None ):
        rcvBufSize = args.receivebuffer
    if( args.quiet != None ):
        quiet = args.quiet

    print("  messagesize is set to     {} bytes".format(messageSize))
    print("  totalcount is set to     ", expectedCount)
    print("  reportinterval is set to ", reportInterval)
    print("  port is set to           ", portNr)
    print("  packetsize is set to      {} bytes".format(packetSize))
    print("  receivebuffer is set to   {} bytes".format(rcvBufSize))
    print("  joinaddress is set to    ", joinAddressString)
    print("  quiet is set to          ", quiet)

    # Set the socket options for multicast receiver
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvBufSize)
    sock.bind(("", portNr))
    multicast_group = socket.inet_aton(joinAddressString)
    mreq = struct.pack('4sL', multicast_group, socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    waitset = socketWaitset(sock)

    # Metrics about the received data
    metrics = udpMetrics(reportInterval)
    percentiles = [100.0, 99.9, 99.0, 90.0]
    expectedMsgIndex = 0
    expectedPacketIndex = 0
    packetsPerMessage = math.ceil( messageSize / packetSize )
    reportCount = reportInterval * packetsPerMessage
    nextAnalyseIndex = reportInterval
    totalPacketsExpected = packetsPerMessage * expectedCount
    totalPackets = 0
    totalMsgs = 0
    subTotalMsgs = 0
    timedOut = 0
    msgIncomplete = 0
    printedError = 0
    receiveTimeOut = RECEIVE_TIMEOUT_SEC_INITIAL
    print("  Waiting for {} messages now...".format(expectedCount))
    while( expectedMsgIndex < expectedCount ):
        # The buffer contains transmitter's data in C struct format, and is received using the socket.
        waitset.wait(receiveTimeOut)
        bytedata, sourceAddress = sock.recvfrom(packetSize)
        (msgIndex, packetIndex, timestamp) = struct.unpack(UDPTESTER_HDRFORMAT, bytedata[:UDPTESTER_HDRSIZE])

        # Check and count received packets.
        if( (msgIndex != expectedMsgIndex) or (packetIndex != expectedPacketIndex) ):
            if(not quiet):
                print("Expected msgIndex {} and packetIndex {}, "
                        "received msgIndex {} and packetIndex {}".format(
                        expectedMsgIndex, expectedPacketIndex,
                        msgIndex, packetIndex))
            expectedMsgIndex = msgIndex
            msgIncomplete = (packetIndex != 0)
        if(packetIndex == (packetsPerMessage - 1)):
            expectedMsgIndex += 1
            expectedPacketIndex = 0
            if(msgIncomplete):
                msgIncomplete = 0
            else:
                totalMsgs += 1
                subTotalMsgs += 1
                metrics.addValue((time.time() - timestamp) * 1e6) # Latency in microseconds
        else:
            expectedPacketIndex = packetIndex + 1
        totalPackets += 1
        reportCount -= 1

        # Report metrics
        if(reportCount == 0):
            print("received {} packets, expecting {} in total".format( 
                totalPackets, totalPacketsExpected))
            reportCount = reportInterval * packetsPerMessage
        if(expectedMsgIndex >= nextAnalyseIndex):
            print("Expecting message with index {}:".format( expectedMsgIndex))
            print("    {:5d} complete messages so far.".format( totalMsgs))
            print("    {:5d} complete messages since the last report.".format( subTotalMsgs))
            metricsReport = metrics.analyse(percentiles)
            for reportItem in metricsReport:
                reportItem.print("    ")
            metrics.reset()
            nextAnalyseIndex = ( expectedMsgIndex + reportInterval -
                                 expectedMsgIndex % reportInterval )
            subTotalMsgs = 0

        if( receiveTimeOut != RECEIVE_TIMEOUT_SEC ):
            receiveTimeOut = RECEIVE_TIMEOUT_SEC
    #end while
    waitset.close()
    sock.close()
    print("Done")
    print("Received {} packets out of {}, lost {:.1f}%".format(
        totalPackets, totalPacketsExpected,
        100.0*float((totalPacketsExpected - totalPackets)/totalPacketsExpected)))
    print("Received {} complete messages out of {}, lost {:.1f}%".format(
        totalMsgs, expectedCount,
        100.0*float((expectedCount - totalMsgs)/expectedCount)))

# signalhandler
def signalHandler(signum, frame):
    print(" Ctrl c was pressed. Exiting...")
    exit(1)
signal.signal(signal.SIGINT, signalHandler)

# shared args
usageString="""
Running the transmitter:
    udpTester.py transmitter address outgoing [-h] [-p PORT] [-m MESSAGESIZE] [-t TOTALCOUNT] [-s PACKETSIZE] [-i INTERVAL] [-l LOSSINESS]

Running the receiver:
    udpTester.py receiver address [-h] [-p PORT] [-m MESSAGESIZE] [-t TOTALCOUNT] [-s PACKETSIZE] [-r REPORTINTERVAL] [-b RECEIVEBUFFER] [-q QUIET]

Note that outgoing is a mandatory argument for the transmitter. It is the network interface address used for outgoing packets.
"""
parser = argparse.ArgumentParser(usage=usageString, description = "Test multicast traffic")
parser.add_argument("role", help="The role selected", type=str, choices=["transmitter", "receiver"])
parser.add_argument("address", help="The ip address to use. Multicast addresses range from 224.0.0.0 to 239.255.255.255", type=str)
parser.add_argument("-p", "--port", help="The port number to use.", type=int)
parser.add_argument("-m", "--messagesize", help="Bytes per message. A message may consist of multiple packets.", type=int)
parser.add_argument("-t", "--totalcount", help="Number of messages", type=int)
parser.add_argument("-s", "--packetsize", help="Bytes per packet sent", type=int)

# transmitter specific args
if( "transmitter" in sys.argv ):
    parser.add_argument("outgoing", help="transmitter mandatory argument: This is the network interface address to use", type=str)
parser.add_argument("-i", "--interval", help="transmitter option: Interval between messages in unit milliseconds.", type=int)
parser.add_argument("-l", "--lossiness", help="transmitter option: Randomly skip sending a packet.", type=int)

# receiver specific args
parser.add_argument("-r", "--reportinterval", help="receiver option: Number of messages per report.", type=int)
parser.add_argument("-b", "--receivebuffer", help="receiver option: receivebuffer size in bytes.", type=int)
parser.add_argument("-q", "--quiet", help="receiver option: Suppress some prints.", type=int)

# Start the transmitter or receiver
args = parser.parse_args()
ipaddressSanitycheck(args.address)
ipaddressMulticastcheck(args.address)
if( args.role == "transmitter" ):
    transmitter(args)
elif( args.role == "receiver" ):
    receiver(args)