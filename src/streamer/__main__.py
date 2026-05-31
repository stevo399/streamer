import logging
import os
import random
import time

import uvicorn

from streamer.config import HOST, PORT
from streamer.pipeline import AudioPipeline
from streamer.scanner import Scanner
from streamer.server import create_app
from streamer.state import ServerState


def main():
    random.seed(os.getpid() ^ int(time.monotonic_ns()) ^ id(object()))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    state = ServerState()
    scanner = Scanner()
    pipeline = AudioPipeline(state, scanner)

    app = create_app(state=state, scanner=scanner, pipeline=pipeline)
    pipeline.start()

    print("Streaming server running")
    print(f"  Control panel: http://localhost:{PORT}")
    print(f"  Stream:        http://localhost:{PORT}/stream.ogg")
    print(f"  API docs:      http://localhost:{PORT}/docs")

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
