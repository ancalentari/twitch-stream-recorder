# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /usr/src/app

# Copy the current directory contents into the container at /usr/src/app
COPY . .

# Install FFmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install Streamlink
RUN pip install --no-cache-dir streamlink

# Copy any additional files, e.g., a script to run Streamlink, if you have one
# COPY your_script.py ./

# Define any environment variables, if needed
# ENV SOME_VARIABLE value

# The command to run your application, replace `your_script.py` with your script file
# CMD ["python", "your_script.py"]
