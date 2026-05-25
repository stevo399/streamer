import logging

from streamer.config import HOST, PORT
from streamer.pipeline import AudioPipeline
from streamer.scanner import Scanner
from streamer.server import create_app
from streamer.state import ServerState


def main():
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

    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
