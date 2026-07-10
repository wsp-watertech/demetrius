FROM mambaorg/micromamba:latest

WORKDIR /app

# Copy environment file
COPY environment.yml .

# Create mamba environment
RUN micromamba install -y -n base -f environment.yml && \
    micromamba clean --all --yes

# Copy project
COPY . .

# Install package in development mode
RUN pip install -e .

ENTRYPOINT ["demetrius"]
