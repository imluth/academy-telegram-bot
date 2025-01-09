#build image
docker build -t academy-telegram-bot .


#run the container
docker run -d --name bs-academy-bot academy-telegram-bot


#exec inside container
docker exec -it mycontainer sh


#pass the environment variable from a .env file
docker run -d --name bs-academy-bot --env-file .env  academy-telegram-bot


#login to docker
docker login

#tag docker image
docker tag academy-telegram-bot:latest imrooot/academy-bot:v4.5


#push image to docker hub
docker push imrooot/academy-bot:v4.5