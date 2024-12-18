#build image
docker build -t academy-bot .


#run the container passing token
docker run -d --name academyTele-bot academy-bot 