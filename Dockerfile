FROM condaforge/miniforge3:25.3.0-3

WORKDIR /app

COPY environment.yml pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN conda env create -f environment.yml && conda clean --all --yes && \
    mkdir -p /work && chown 10001:0 /work

ENV PATH="/opt/conda/envs/demetrius/bin:${PATH}" \
    PROJ_DATA=/opt/conda/envs/demetrius/share/proj \
    PROJ_NETWORK=OFF \
    PYTHONUNBUFFERED=1 \
    DEMETRIUS_DATA_DIR=/tmp/demetrius_tiles \
    TMPDIR=/tmp \
    CPL_TMPDIR=/tmp

USER 10001:0
WORKDIR /work

ENTRYPOINT ["demetrius"]
