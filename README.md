# SlitScan
###### Active Open Proxy Harvesting & Testing

SlitScan harvests addresses of proxies from various web sources, tests them, and 
logs the results. Proxies are tested by asking to connect-back to SlitScan. Active 
detection is the only way to reliably detect proxies: specifically those running 
on non-standard ports, and especially proxy-tunnels which expose no ports at all.

The primary purpose of SlitScan is to address the problem of proxy-tunnels, which 
are proxies that have a different input and output IP address. These proxies are 
undetectable when a program like BOPM scans the output IP, where no ports are 
usually open. Tunnels are found by feeding the input side a random token and 
listening for it from one of the connect-backs.


### Layout

	slitscan.py     - A daemon that accepts input from Harvesters and conducts the testing.
	harvest/        - Directory with the harvester scripts.


### Quick Start

1. Run slitscan.py, optionally with a connect-back ip:port.

		./slitscan.py                     (defaults to 0.0.0.0:16667)
		./slitscan.py 1.2.3.4:1234        (tells proxies to connect-back to that instead)

2. Run one or more harvesters in the harvest directory.


### Getting Results

slitscan.py will output results to console and simultaneously append to a log 
file. The file is easily parsed or grepable to extract the data you need. There is 
a symbol column that represents each phase of detection and can be grep'ed.

Some important symbols:

	><    - A successful connect-back from a proxy using the same input and output IP.
	()    - A successful tunnel detection, and this is the input side of the tunnel.
	)(    - The output side of an associated tunnel.


For example:

	grep "()" slitscan.log

The above dumps all found proxy-tunnels which you can then pipe to `cut` `sort`
and `uniq` etc as needed.
