from __future__ import annotations

import argparse, math, socket, time, uuid
from .edge_protocol import batch, decode_frame, encode_frame, hello


def main() -> None:
    p=argparse.ArgumentParser(description="Simulate an ESP Ethernet DAQ")
    p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=9100)
    p.add_argument("--device",default="ESP-DAQ-01"); p.add_argument("--batches",type=int,default=200); args=p.parse_args()
    boot=str(uuid.uuid4()); rate=1000; size=50; period_us=1_000_000//rate
    with socket.create_connection((args.host,args.port),timeout=3) as sock:
        file=sock.makefile("rwb"); file.write(encode_frame(hello(args.device,boot,"sim-1.0",["pressure_raw","thrust_raw","temperature_raw"]))); file.flush(); print(decode_frame(file.readline()))
        for sequence in range(args.batches):
            base=sequence*size/rate; pressure=[]; thrust=[]; temperature=[]
            for offset in range(size):
                t=base+offset/rate; envelope=min(1,t/.35) if t<.35 else (1 if t<6.8 else max(0,(8-t)/1.2))
                pressure.append(round(61.5*envelope+0.08*math.sin(t*37),3)); thrust.append(round(435*envelope+0.7*math.sin(t*19),2)); temperature.append(round(28+min(t,8)*5.2,2))
            msg=batch(args.device,boot,sequence,int(base*1_000_000),period_us,{"pressure_raw":pressure,"thrust_raw":thrust,"temperature_raw":temperature})
            file.write(encode_frame(msg)); file.flush(); ack=decode_frame(file.readline())
            if sequence%20==0: print(f"batch={sequence} ack={ack.get('ack_sequence')}")
            time.sleep(size/rate)


if __name__ == "__main__": main()
