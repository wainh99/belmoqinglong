FROM eclipse-temurin:17-jre
WORKDIR /app
COPY main.jar ./
EXPOSE 8080
CMD ["java", "-jar", "main.jar"]
