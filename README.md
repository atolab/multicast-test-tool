## Multicast tool

The purpose of this tool is to validate a network's capability for multicast communication, so that one can confirm
that the network communication requirements of DDS are fulfilled. It is intended to be simple to use, readable,
and "just work" out of the box with no complicated dependencies.

The application can perform one of two roles, the transmitter or the receiver, which can be chosen by starting the tool
with the keyword argument `transmitter` or `receiver`, respectively. To validate the multicast communication,
the transmitter sends a defined set of data, and the receiver receives and checks if that defined set of data
is received properly, or possibly there were losses or duplicates.

The transmitter and receiver processes may be on different virtual or physical machines on the same network,
and more than one receiver can be used at the same time to receive data from one transmitter.
The user is expected to know what network they want to test, so the arguments for the multicast address
and the network interface are mandatory. The optional arguments are mostly for playing with parameters such as
message count, size, frequency.


```
transmitter:
    usage: udpTester.py transmitter [-h] [-a ADDRESS] [-i INTERFACE] [-p PORT] [-t TOTALCOUNT] [-m MESSAGESIZE] [-s PACKETSIZE]
                                    [-f FREQUENCY] [-l LOSSINESS]

    Send messages via multicast. If the receiver receives all of them, the network has passed the test.

    optional arguments:
      -h, --help            show this help message and exit
      -a ADDRESS, --address ADDRESS
                            Mandatory argument: The ip address to use. Multicast addresses range from 224.0.0.0 to 239.255.255.255
      -i INTERFACE, --interface INTERFACE
                            Mandatory argument: This is the network interface address to use
      -p PORT, --port PORT  The port number to use.
      -t TOTALCOUNT, --totalcount TOTALCOUNT
                            Number of messages
      -m MESSAGESIZE, --messagesize MESSAGESIZE
                            Bytes per message. A message may consist of multiple packets.
      -s PACKETSIZE, --packetsize PACKETSIZE
                            Bytes per packet sent
      -f FREQUENCY, --frequency FREQUENCY
                            transmitter option: Frequency of sending messages in unit Hz.
      -l LOSSINESS, --lossiness LOSSINESS
                            transmitter option: Randomly skip sending a packet, chance in unit %.

    Example:
        python3 udpTester.py transmitter -a 239.0.0.1 -i 192.168.2.33 -t 200 -m 450 -s 150 -f 60
    


receiver:
    usage: udpTester.py receiver [-h] [-a ADDRESS] [-i INTERFACE] [-p PORT] [-t TOTALCOUNT] [-m MESSAGESIZE] [-s PACKETSIZE]
                                 [-b RECEIVEBUFFER] [-r REPORTINTERVAL]

    Receive multicast messages. If I receive all messages from the transmitter, the network has passed the test.
    The results will also show latency values in microseconds, calculated as current_time - source_timestamp. These values
    are not reliable when transmitter's and receiver's clocks are not precisely synchronized, and may be negative when the 
    receiver's clock is trailing the transmitter's clock.
    

    optional arguments:
      -h, --help            show this help message and exit
      -a ADDRESS, --address ADDRESS
                            Mandatory argument: The ip address to use. Multicast addresses range from 224.0.0.0 to 239.255.255.255
      -i INTERFACE, --interface INTERFACE
                            Mandatory argument: This is the network interface address to use
      -p PORT, --port PORT  The port number to use.
      -t TOTALCOUNT, --totalcount TOTALCOUNT
                            Number of messages
      -m MESSAGESIZE, --messagesize MESSAGESIZE
                            Bytes per message. A message may consist of multiple packets.
      -s PACKETSIZE, --packetsize PACKETSIZE
                            Bytes per packet sent
      -b RECEIVEBUFFER, --receivebuffer RECEIVEBUFFER
                            receiver option: receivebuffer size in bytes.
      -r REPORTINTERVAL, --reportinterval REPORTINTERVAL
                            receiver option: Number of messages per report.

    Example:
        python3 udpTester.py receiver -a 239.0.0.1 -i 192.168.2.33 -t 200 -m 450 -s 150 -b 200000
```