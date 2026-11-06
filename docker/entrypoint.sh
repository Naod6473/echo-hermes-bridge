#!/bin/sh
# Download the Piper voice named in PIPER_VOICE into /data/voices on first start, then run the bridge.
set -e
voice=$(basename "${PIPER_VOICE:-en_US-lessac-medium.onnx}" .onnx)
mkdir -p /data/voices
if [ ! -f "/data/voices/$voice.onnx" ]; then
    echo "downloading Piper voice $voice"
    (cd /data/voices && python -m piper.download_voices "$voice") || echo "warning: could not download voice $voice"
fi
export PIPER_VOICE="/data/voices/$voice.onnx"
exec python /app/bridge.py "$@"
