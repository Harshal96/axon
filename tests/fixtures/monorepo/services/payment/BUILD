py_library(
    name = "payment_lib",
    srcs = ["src/payment.py", "src/models.py"],
    deps = [
        "//internal/models:models",
        ":payment_gateway_java",
    ],
    visibility = ["//..."],
)

genrule(
    name = "payment_gateway_java",
    srcs = ["pom.xml"],
    outs = ["payment_gateway.jar"],
    cmd = "touch $@",
    visibility = ["//services/payment:__pkg__"],
)

genrule(
    name = "generate_payment_client_py",
    srcs = ["swagger.json"],
    outs = ["generated/payment_client.py"],
    cmd = "touch $@",
    visibility = ["//services/checkout:__pkg__", "//clients/payment:__pkg__"],
)

py_test(
    name = "payment_test",
    srcs = ["src/payment_test.py"],
    deps = [":payment_lib"],
    testonly = True,
)
