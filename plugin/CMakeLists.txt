cmake_minimum_required(VERSION 3.5)

if(NOT DEFINED CMAKE_TOOLCHAIN_FILE)
  if(DEFINED ENV{VITASDK})
    set(CMAKE_TOOLCHAIN_FILE "$ENV{VITASDK}/share/vita.toolchain.cmake" CACHE PATH "toolchain file")
  else()
    message(FATAL_ERROR "Bitte VITASDK setzen")
  endif()
endif()

project(vytp C)
include("$ENV{VITASDK}/share/vita.cmake" REQUIRED)

set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -std=gnu11 -Wall -O2 -nostdlib")

# Lader: KEINE SceNet-Importe, damit er immer geladen wird
add_executable(vytp loader.c)
target_link_libraries(vytp
  taihen_stub
  SceLibKernel_stub
  SceKernelModulemgr_stub
  SceKernelThreadMgr_stub
  SceIofilemgr_stub
  SceSysmodule_stub
  gcc
)
set_target_properties(vytp PROPERTIES LINK_FLAGS "-nostdlib")
vita_create_self(vytp.suprx vytp CONFIG ${CMAKE_SOURCE_DIR}/exports.yml UNSAFE)

# Server: wird vom Lader nachgeladen, wenn SceNet da ist
add_executable(vytp_net net.c)
target_link_libraries(vytp_net
  SceLibKernel_stub
  SceKernelThreadMgr_stub
  SceIofilemgr_stub
  SceNet_stub
  SceHttp_stub
  gcc
)
set_target_properties(vytp_net PROPERTIES LINK_FLAGS "-nostdlib")
vita_create_self(vytp_net.suprx vytp_net CONFIG ${CMAKE_SOURCE_DIR}/exports_net.yml UNSAFE)

# Diagnose-Hooks (optional): ux0:tai/vytp_diag.suprx wird vom Lader geladen, falls vorhanden
add_executable(vytp_diag diag.c)
target_link_libraries(vytp_diag taihen_stub SceLibKernel_stub SceIofilemgr_stub gcc)
set_target_properties(vytp_diag PROPERTIES LINK_FLAGS "-nostdlib")
vita_create_self(vytp_diag.suprx vytp_diag CONFIG ${CMAKE_SOURCE_DIR}/exports_diag.yml UNSAFE)
