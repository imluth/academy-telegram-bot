#build image
docker build -t academy-telegram-bot .


#run the container
docker run -d --name bs-academy-bot academy-telegram-bot


#exec inside container
docker exec -it mycontainer sh


#pass the environment variable from a .env file
docker run -d --name bs-academy-bot --env-file .env  academy-telegram-bot

===================================