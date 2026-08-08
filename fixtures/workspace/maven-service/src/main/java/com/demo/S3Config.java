package com.demo;
import software.amazon.awssdk.services.s3.S3Client;
public class S3Config { public S3Client client() { return S3Client.builder().build(); } }
